"""Flights agent — fetches one-way flight prices KSC -> WAW/WMI.

Primary source: Sky-Scrapper API via RapidAPI (sky-scrapper.p.rapidapi.com).
This is the "Air Scraper"-style endpoint family — uses a two-id system
(skyId + entityId per airport). We hardcode the IDs for our three airports
(resolved once via /api/v1/flights/searchAirport); refresh them via that
endpoint if anything stops working. Stores a route-hash trip_id so price
history attributes to the same route+carrier over time.

In production this NEVER returns mock data — empty results propagate so the
critic agent can flag "no transport available". When the configured outbound
date has no flights, the agent tries ±1 day and tags each result with
`date_offset_days` so the analyser can present alternatives.

Run standalone:
  python agents/flights_agent.py --dry-run
  python agents/flights_agent.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import urllib.parse as _u
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.config_loader import load_config  # noqa: E402

DEFAULT_RAPIDAPI_HOST = "sky-scrapper.p.rapidapi.com"
DEFAULT_SEARCH_PATH = "/api/v1/flights/searchFlights"

# Sky-Scrapper requires both skyId (IATA-like) AND entityId per airport.
# Resolved via /api/v1/flights/searchAirport; refresh if airports change.
AIRPORT_IDS: dict[str, dict[str, str]] = {
    "KSC": {"skyId": "KSC", "entityId": "104120247"},  # Košice
    "WAW": {"skyId": "WAW", "entityId": "95673438"},   # Warsaw Chopin
    "WMI": {"skyId": "WMI", "entityId": "128667439"},  # Warsaw Modlin
}

_LAST_ERRORS: list[str] = []


def _env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _rapidapi_key() -> str | None:
    """Return the Sky Scrapper-specific RapidAPI key, falling back to shared key."""
    return _env_value("RAPIDAPI_SKY_KEY") or _env_value("RAPIDAPI_KEY")


def _rapidapi_host() -> str:
    """Return the RapidAPI host for the subscribed Sky Scrapper API product."""
    return _env_value("RAPIDAPI_SKY_HOST") or DEFAULT_RAPIDAPI_HOST


def _search_url() -> str:
    path = _env_value("RAPIDAPI_SKY_FLIGHTS_PATH") or DEFAULT_SEARCH_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    return f"https://{_rapidapi_host()}{path}"


def _is_supported_sky_api() -> bool:
    """Return false for generic RapidAPI scraper products that lack flight endpoints."""
    host = _rapidapi_host()
    if host == "sky-scrapper3.p.rapidapi.com":
        msg = (
            "RAPIDAPI_SKY_HOST=sky-scrapper3.p.rapidapi.com is a generic POST /scrape "
            "API, not the Sky-Scrapper flights API. Use a RapidAPI subscription whose "
            "sample code calls /api/v1/flights/searchFlights."
        )
        print(f"[flights] {msg}")
        _LAST_ERRORS.append(msg)
        return False
    return True


def get_last_errors() -> list[str]:
    """Return + clear warnings recorded by the most recent fetch."""
    out = list(_LAST_ERRORS)
    _LAST_ERRORS.clear()
    return out


def _trip_id(origin: str, dest: str, date: str, carrier: str) -> str:
    code = "".join(c for c in carrier.upper() if c.isalpha())[:2] or "XX"
    return f"{origin}-{dest}-{date.replace('-', '')}-{code}"


def _skyscanner_search_link(origin: str, dest: str, date: str, adults: int) -> str:
    """Build a Skyscanner search URL with route/date/passengers pre-filled."""
    return (
        f"https://www.skyscanner.com/transport/flights/{origin.lower()}/{dest.lower()}/"
        f"{date.replace('-', '')[2:]}/?adults={adults}"
    )


def _is_verified_direct_market(carrier: str, origin: str, dest: str) -> bool:
    """Return true only for direct airline markets we have verified.

    Aggregators may return low-cost self-transfer itineraries where the first
    marketing carrier is Wizz/Ryanair, but those airlines reject direct
    KSC-WAW/WMI checkout URLs. For unverified markets, link to Skyscanner
    search instead of manufacturing an airline checkout URL.
    """
    c = (carrier or "").lower()
    if origin == "KSC" and dest == "WAW" and ("lot" in c or c == "lo"):
        return True
    return False


def _deep_link(
    carrier: str,
    origin: str,
    dest: str,
    date: str,
    adults: int,
    stops: int,
    fallback: str | None,
) -> str:
    """Build the safest booking/search link with date / pax pre-filled."""
    if fallback:
        return fallback

    # Carrier checkout links are only safe for verified non-stop markets.
    if stops != 0 or not _is_verified_direct_market(carrier, origin, dest):
        return _skyscanner_search_link(origin, dest, date, adults)

    c = (carrier or "").lower()
    if "ryanair" in c:
        q = _u.urlencode({
            "adults": adults, "teens": 0, "children": 0, "infants": 0,
            "dateOut": date, "originIata": origin, "destinationIata": dest,
            "isReturn": "false",
        })
        return f"https://www.ryanair.com/gb/en/trip/flights/select?{q}"
    if "wizz" in c:
        return (
            f"https://wizzair.com/en-gb/booking/select-flight/"
            f"{origin}/{dest}/{date}/null/{adults}/0/0/null"
        )
    return _skyscanner_search_link(origin, dest, date, adults)


def _normalise(raw: dict[str, Any], origin: str, dest: str, date: str, offset: int, adults: int) -> dict[str, Any]:
    carrier = raw.get("carrier", "Unknown")
    return {
        "trip_id": _trip_id(origin, dest, date, carrier),
        "type": "flight",
        "carrier": carrier,
        "price_eur_per_person": raw.get("price_eur"),
        "duration_min": raw.get("duration_min"),
        "departure": raw.get("departure"),
        "arrival": raw.get("arrival"),
        "stops": raw.get("stops", 0),
        "booking_link": _deep_link(
            carrier, origin, dest, date, adults, raw.get("stops", 0), raw.get("booking_link")
        ),
        "date": date,
        "date_offset_days": offset,
    }


def _query_skyscanner(origin: str, dest: str, date: str, max_stops: int) -> list[dict[str, Any]]:
    """Return raw flight items (unnormalised) for a single OD pair + date.

    Always queries with adults=1 to get a stable per-person price; the group
    size only affects the deep link, not the priced quote.
    """
    import requests

    origin_ids = AIRPORT_IDS.get(origin)
    dest_ids = AIRPORT_IDS.get(dest)
    if not origin_ids or not dest_ids:
        msg = f"No Sky-Scrapper IDs hardcoded for {origin}->{dest}; refresh AIRPORT_IDS."
        print(f"[flights] {msg}")
        _LAST_ERRORS.append(msg)
        return []

    key = _rapidapi_key()
    if not key:
        print("[flights] RAPIDAPI_KEY/RAPIDAPI_SKY_KEY not set - returning empty.")
        return []
    host = _rapidapi_host()
    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": host,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.get(
            _search_url(), headers=headers,
            params={
                "originSkyId": origin_ids["skyId"],
                "destinationSkyId": dest_ids["skyId"],
                "originEntityId": origin_ids["entityId"],
                "destinationEntityId": dest_ids["entityId"],
                "date": date,
                "adults": "1",
                "currency": "EUR",
                "cabinClass": "economy",
                "sortBy": "best",
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            msg = (
                f"Sky-Scrapper API HTTP {resp.status_code} via host {host} for "
                f"{origin}->{dest} {date}: {resp.text[:200]}"
            )
            print(f"[flights] {msg}")
            _LAST_ERRORS.append(msg)
            return []
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        msg = f"Sky-Scrapper API call failed ({origin}->{dest} {date}): {type(e).__name__}: {e}"
        print(f"[flights] {msg}")
        _LAST_ERRORS.append(msg)
        return []

    out = []
    itineraries = (payload.get("data") or {}).get("itineraries", [])
    for item in itineraries:
        legs = item.get("legs") or [{}]
        leg = legs[0]
        stops = leg.get("stopCount", 0)
        if stops > max_stops:
            continue
        marketing = (leg.get("carriers") or {}).get("marketing") or [{}]
        carrier = marketing[0].get("name") if marketing else None
        dep = leg.get("departure") or ""
        arr = leg.get("arrival") or ""
        out.append({
            "carrier": carrier or "Unknown",
            "price_eur": (item.get("price") or {}).get("raw"),
            "duration_min": leg.get("durationInMinutes"),
            # Trim ISO "2026-08-07T15:05:00" -> "15:05"
            "departure": dep[11:16] if len(dep) >= 16 else dep,
            "arrival": arr[11:16] if len(arr) >= 16 else arr,
            "stops": stops,
            # Sky-Scrapper does not return an itinerary deeplink; _deep_link()
            # builds either a verified airline link or a safer Skyscanner search.
            "booking_link": None,
        })
    return out


def _fetch_live(config: dict[str, Any]) -> list[dict[str, Any]]:
    trip = config["trip"]
    origin = trip["origin_airport"]
    base_date = dt.date.fromisoformat(trip["dates"]["outbound"])
    link_adults = trip["group_size"]      # used only when building booking URLs
    max_stops = config["transport"]["max_layovers"]
    dests = [trip["destination_airport"], "WMI"]

    # Try the configured date first; widen by one day at a time and return at the
    # first offset that yields anything. Bounds API calls at 2/4/6 instead of always 6.
    for offset in (0, -1, 1):
        date_iso = (base_date + dt.timedelta(days=offset)).isoformat()
        batch: list[dict[str, Any]] = []
        for dest in dests:
            for raw in _query_skyscanner(origin, dest, date_iso, max_stops):
                batch.append(_normalise(raw, origin, dest, date_iso, offset, link_adults))
        if batch:
            return batch
    return []


def _mock_data(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic sample used by --dry-run ONLY."""
    trip = config["trip"]
    origin = trip["origin_airport"]
    date = trip["dates"]["outbound"]
    adults = trip["group_size"]
    samples = [
        {"carrier": "LOT", "price_eur": 119, "duration_min": 55,
         "departure": "17:40", "arrival": "18:35", "stops": 0, "dest": "WAW"},
    ]
    return [_normalise(s, origin, s["dest"], date, 0, adults) for s in samples]


def fetch_flights(config: dict[str, Any], dry_run: bool = False) -> list[dict[str, Any]]:
    if dry_run:
        return _mock_data(config)
    if not _rapidapi_key():
        print("[flights] RAPIDAPI_KEY/RAPIDAPI_SKY_KEY not set — returning empty.")
        return []
    if not _is_supported_sky_api():
        return []
    return _fetch_live(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch flight options")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import json

    print(json.dumps(fetch_flights(load_config(), dry_run=args.dry_run), indent=2, ensure_ascii=False))

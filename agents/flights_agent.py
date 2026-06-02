"""Flights agent — fetches one-way flight prices KSC -> WAW/WMI.

Primary source: Skyscanner Flights Search API via RapidAPI
(skyscanner50.p.rapidapi.com). Stores a route-hash trip_id so price history
attributes to the same route+carrier over time.

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

RAPIDAPI_HOST = "skyscanner50.p.rapidapi.com"
SEARCH_URL = f"https://{RAPIDAPI_HOST}/api/v1/searchFlights"


def _trip_id(origin: str, dest: str, date: str, carrier: str) -> str:
    code = "".join(c for c in carrier.upper() if c.isalpha())[:2] or "XX"
    return f"{origin}-{dest}-{date.replace('-', '')}-{code}"


def _deep_link(carrier: str, origin: str, dest: str, date: str, adults: int, fallback: str | None) -> str:
    """Build a carrier-specific deep link with date / pax pre-filled."""
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
    # Generic Skyscanner deeplink fallback (the API returns one).
    return fallback or (
        f"https://www.skyscanner.com/transport/flights/{origin.lower()}/{dest.lower()}/"
        f"{date.replace('-', '')[2:]}/?adults={adults}"
    )


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
        "booking_link": _deep_link(carrier, origin, dest, date, adults, raw.get("booking_link")),
        "date": date,
        "date_offset_days": offset,
    }


def _query_skyscanner(origin: str, dest: str, date: str, max_stops: int) -> list[dict[str, Any]]:
    """Return raw flight items (unnormalised) for a single OD pair + date.

    Always queries with adults=1 to get a stable per-person price; the group
    size only affects the deep link, not the priced quote.
    """
    import requests

    key = os.environ["RAPIDAPI_KEY"]
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": RAPIDAPI_HOST}
    try:
        resp = requests.get(
            SEARCH_URL,
            headers=headers,
            params={
                "origin": origin, "destination": dest, "date": date,
                "adults": "1", "currency": "EUR",
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return []

    out = []
    for item in payload.get("data", []):
        legs = item.get("legs") or [{}]
        stops = legs[0].get("stops", 0)
        if stops > max_stops:
            continue
        out.append({
            "carrier": (legs[0].get("carriers") or ["Unknown"])[0],
            "price_eur": (item.get("price") or {}).get("amount"),
            "duration_min": legs[0].get("durationInMinutes"),
            "departure": legs[0].get("departure"),
            "arrival": legs[0].get("arrival"),
            "stops": stops,
            "booking_link": item.get("deeplink") or item.get("url"),
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
        {"carrier": "Wizz Air", "price_eur": 54, "duration_min": 100,
         "departure": "12:10", "arrival": "13:50", "stops": 0, "dest": "WMI"},
    ]
    return [_normalise(s, origin, s["dest"], date, 0, adults) for s in samples]


def fetch_flights(config: dict[str, Any], dry_run: bool = False) -> list[dict[str, Any]]:
    if dry_run:
        return _mock_data(config)
    if not os.environ.get("RAPIDAPI_KEY"):
        print("[flights] RAPIDAPI_KEY not set — returning empty.")
        return []
    return _fetch_live(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch flight options")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import json

    print(json.dumps(fetch_flights(load_config(), dry_run=args.dry_run), indent=2, ensure_ascii=False))

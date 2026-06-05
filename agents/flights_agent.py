"""Flights agent — fetches one-way flight prices KSC -> WAW/WMI.

Primary source: Kiwi.com Flights API via RapidAPI.
The configured endpoint is `/api/v1/flights/price-map`, which returns
structured indicative prices for destinations inside a geographic bounding box.
It is not a live itinerary search, so schedule fields may be unavailable. When
missing, this agent leaves departure/arrival/duration as None instead of
fabricating them. Stores a route-hash trip_id so price history attributes to
the same route+carrier over time.

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
from functools import lru_cache
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.kiwi import KiwiAutocompleteResponse, KiwiPriceMapResponse  # noqa: E402
from utils.config_loader import load_config  # noqa: E402

KIWI_HOST = "kiwi-com-flights-api.p.rapidapi.com"
KIWI_PRICE_MAP_PATH = "/api/v1/flights/price-map"

# RapidAPI's Kiwi price-map source uses location slugs like the documentation
# sample `london-united-kingdom`.
KIWI_SOURCE_SLUGS: dict[str, str] = {
    "KSC": "kosice-international-kosice-slovakia",
}

KIWI_PLACE_QUERIES: dict[str, str] = {
    "KSC": "kosice",
    "WAW": "warsaw",
    "WMI": "modlin",
}

DESTINATION_HINTS: dict[str, set[str]] = {
    "WAW": {"waw", "warsaw", "warszawa", "warsaw-poland", "chopin"},
    "WMI": {"wmi", "modlin"},
}

# Broad Poland/Central-Europe box so Warsaw and Modlin are included.
KIWI_POLAND_BOUNDING_BOX = "49,14,55,25"

_LAST_ERRORS: list[str] = []


def _env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _rapidapi_key() -> str | None:
    """Return the Kiwi-specific RapidAPI key, falling back to the shared key."""
    return _env_value("RAPIDAPI_KIWI_KEY") or _env_value("RAPIDAPI_KEY")


def _kiwi_host() -> str:
    """Return the RapidAPI host for the Kiwi.com Flights API product."""
    return _env_value("RAPIDAPI_KIWI_HOST") or KIWI_HOST


def _kiwi_price_map_url() -> str:
    path = _env_value("RAPIDAPI_KIWI_PRICE_MAP_PATH") or KIWI_PRICE_MAP_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    return f"https://{_kiwi_host()}{path}"


def _kiwi_autocomplete_url() -> str:
    path = _env_value("RAPIDAPI_KIWI_AUTOCOMPLETE_PATH") or "/api/v1/places/autocomplete"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"https://{_kiwi_host()}{path}"


def get_last_errors() -> list[str]:
    """Return + clear warnings recorded by the most recent fetch."""
    out = list(_LAST_ERRORS)
    _LAST_ERRORS.clear()
    return out


def _trip_id(origin: str, dest: str, date: str, carrier: str) -> str:
    code = "".join(c for c in carrier.upper() if c.isalpha())[:2] or "XX"
    return f"{origin}-{dest}-{date.replace('-', '')}-{code}"


def _kiwi_search_link(origin_slug: str, dest_slug: str, date: str, adults: int) -> str:
    """Build a Kiwi.com search URL with route/date/passengers pre-filled."""
    q = _u.urlencode({"adults": adults})
    return f"https://www.kiwi.com/en/search/results/{origin_slug}/{dest_slug}/{date}/no-return?{q}"


def _headers() -> dict[str, str] | None:
    key = _rapidapi_key()
    if not key:
        print("[flights] RAPIDAPI_KEY not set - returning empty.")
        return None
    return {
        "x-rapidapi-key": key,
        "x-rapidapi-host": _kiwi_host(),
        "Content-Type": "application/json",
    }


@lru_cache(maxsize=16)
def _resolve_kiwi_place_slug(code: str) -> str | None:
    """Resolve an IATA code to Kiwi's place slug via autocomplete."""
    import requests

    headers = _headers()
    if not headers:
        return None

    query = KIWI_PLACE_QUERIES.get(code, code)
    try:
        resp = requests.get(
            _kiwi_autocomplete_url(),
            headers=headers,
            params={
                "locale": "en-us",
                "query": query,
                "limit": "10",
                "types": "CITY,STATION,AIRPORT",
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            msg = f"Kiwi autocomplete HTTP {resp.status_code} for {code}: {resp.text[:200]}"
            print(f"[flights] {msg}")
            _LAST_ERRORS.append(msg)
            return KIWI_SOURCE_SLUGS.get(code)
        parsed = KiwiAutocompleteResponse.model_validate(resp.json())
    except (requests.RequestException, ValueError) as e:
        msg = f"Kiwi autocomplete failed for {code}: {type(e).__name__}: {e}"
        print(f"[flights] {msg}")
        _LAST_ERRORS.append(msg)
        return KIWI_SOURCE_SLUGS.get(code)

    code_lower = code.casefold()
    exact = next((p for p in parsed.places if (p.code or "").casefold() == code_lower), None)
    place = exact or (parsed.places[0] if parsed.places else None)
    if not place:
        msg = f"Kiwi autocomplete returned no place for {code}."
        print(f"[flights] {msg}")
        _LAST_ERRORS.append(msg)
        return KIWI_SOURCE_SLUGS.get(code)
    return place.slug


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
    origin_slug = _resolve_kiwi_place_slug(origin) or origin.lower()
    dest_slug = _resolve_kiwi_place_slug(dest) or dest.lower()
    return _kiwi_search_link(origin_slug, dest_slug, date, adults)


def _normalise(raw: dict[str, Any], origin: str, dest: str, date: str, offset: int, adults: int) -> dict[str, Any]:
    carrier = raw.get("carrier", "Unknown")
    origin_slug = raw.get("origin_slug") or _resolve_kiwi_place_slug(origin) or origin.lower()
    dest_slug = raw.get("destination_slug") or _resolve_kiwi_place_slug(dest) or dest.lower()
    booking_link = raw.get("booking_link") or _kiwi_search_link(origin_slug, dest_slug, date, adults)
    return {
        "trip_id": _trip_id(origin, dest, date, carrier),
        "type": "flight",
        "carrier": carrier,
        "price_eur_per_person": raw.get("price_eur"),
        "duration_min": raw.get("duration_min"),
        "departure": raw.get("departure"),
        "arrival": raw.get("arrival"),
        "stops": raw.get("stops"),
        "booking_link": booking_link,
        "date": date,
        "date_offset_days": offset,
    }


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("amount", "value", "raw", "price", "minPrice"):
            parsed = _coerce_float(value.get(key))
            if parsed is not None:
                return parsed
        return None
    digits = "".join(c for c in str(value) if c.isdigit() or c == ".")
    return float(digits) if digits else None


def _coerce_int(value: Any) -> int | None:
    parsed = _coerce_float(value)
    return int(parsed) if parsed is not None else None


def _iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _string_values(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            out.extend(_string_values(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(_string_values(child))
    elif isinstance(value, str):
        out.append(value.casefold())
    return out


def _matches_destination(item: dict[str, Any], dest: str) -> bool:
    hints = DESTINATION_HINTS.get(dest, {dest.casefold()})
    haystack = " ".join(_string_values(item))
    return any(hint in haystack for hint in hints)


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    for child in item.values():
        if isinstance(child, dict):
            found = _first_value(child, keys)
            if found not in (None, ""):
                return found
    return None


def _extract_price(item: dict[str, Any]) -> float | None:
    for key in ("price", "amount", "value", "minPrice", "minimumPrice"):
        parsed = _coerce_float(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _normalise_time(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) >= 16 and text[10] in ("T", " "):
        return text[11:16]
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return text


def _query_kiwi_price_map(origin: str, dest: str, date: str, max_stops: int) -> list[dict[str, Any]]:
    """Return raw flight items (unnormalised) for a single OD pair + date.

    Kiwi price-map is indicative, so many items may only include a destination
    and price. The parser is intentionally permissive and filters candidates
    to Warsaw/WAW/WMI when the response contains richer destination fields.
    """
    import requests

    headers = _headers()
    if not headers:
        return []

    host = _kiwi_host()
    source = _resolve_kiwi_place_slug(origin)
    if not source:
        msg = f"No Kiwi source slug configured for {origin}; add it to KIWI_SOURCE_SLUGS."
        print(f"[flights] {msg}")
        _LAST_ERRORS.append(msg)
        return []

    try:
        resp = requests.get(
            _kiwi_price_map_url(),
            headers=headers,
            params={
                "source": source,
                "currency": "EUR",
                "start_date": date,
                "end_date": date,
                "bounding_box": _env_value("RAPIDAPI_KIWI_BOUNDING_BOX") or KIWI_POLAND_BOUNDING_BOX,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            msg = (
                f"Kiwi.com Flights API HTTP {resp.status_code} via host {host} for "
                f"{origin}->{dest} {date}: {resp.text[:200]}"
            )
            print(f"[flights] {msg}")
            _LAST_ERRORS.append(msg)
            return []
        payload = KiwiPriceMapResponse.model_validate(resp.json()).model_dump()
    except (requests.RequestException, ValueError) as e:
        msg = f"Kiwi.com Flights API call failed ({origin}->{dest} {date}): {type(e).__name__}: {e}"
        print(f"[flights] {msg}")
        _LAST_ERRORS.append(msg)
        return []

    out = []
    for item in _iter_dicts(payload):
        price = _extract_price(item)
        if price is None or not _matches_destination(item, dest):
            continue
        stops = _coerce_int(_first_value(item, ("stops", "stopCount", "numberOfStops")))
        if stops is not None and stops > max_stops:
            continue
        carrier = _first_value(item, ("airline", "airlines", "carrier", "carrierName", "company"))
        if isinstance(carrier, list):
            carrier = ", ".join(str(c) for c in carrier if c)
        link = _first_value(item, ("deep_link", "deepLink", "booking_link", "bookingLink", "url", "link"))
        destination = item.get("destination") if isinstance(item.get("destination"), dict) else {}
        out.append({
            "carrier": str(carrier) if carrier else "Kiwi.com",
            "price_eur": price,
            "duration_min": _coerce_int(_first_value(item, ("duration", "duration_min", "durationMinutes"))),
            "departure": _normalise_time(_first_value(item, ("departure", "departureTime", "local_departure"))),
            "arrival": _normalise_time(_first_value(item, ("arrival", "arrivalTime", "local_arrival"))),
            "stops": stops,
            "booking_link": str(link) if link else None,
            "origin_slug": source,
            "destination_slug": destination.get("slug"),
        })
    return _dedupe_raw(out)


def _dedupe_raw(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        key = (
            item.get("carrier"),
            item.get("price_eur"),
            item.get("departure"),
            item.get("arrival"),
            item.get("booking_link"),
        )
        current = deduped.get(key)
        if current is None or (item.get("duration_min") or 1e9) < (current.get("duration_min") or 1e9):
            deduped[key] = item
    return list(deduped.values())


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
            for raw in _query_kiwi_price_map(origin, dest, date_iso, max_stops):
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
        print("[flights] RAPIDAPI_KEY/RAPIDAPI_KIWI_KEY not set — returning empty.")
        return []
    return _fetch_live(config)


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Fetch flight options")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import json

    print(json.dumps(fetch_flights(load_config(), dry_run=args.dry_run), indent=2, ensure_ascii=False))

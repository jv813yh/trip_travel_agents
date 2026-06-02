"""FlixBus agent — fetches bus prices Košice -> Warsaw for the outbound date.

Source: FlixBus API via RapidAPI (flixbus.p.rapidapi.com).
City IDs: Košice = 39, Warsaw = 36 (per CLAUDE.md).

In production, returns [] when no trips are found — never falls back to mock
data. When the configured date has no trips, the agent tries ±1 day and tags
each result with `date_offset_days` so the analyser can present alternatives.

Run standalone:
  python agents/flixbus_agent.py --dry-run
  python agents/flixbus_agent.py
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

RAPIDAPI_HOST = "flixbus.p.rapidapi.com"
SEARCH_URL = f"https://{RAPIDAPI_HOST}/search_trips"
KOSICE_CITY_ID = "39"
WARSAW_CITY_ID = "36"


def _trip_id(date: str, idx: int) -> str:
    return f"KOS-WAW-{date.replace('-', '')}-FX{idx}"


def _deep_link(date: str, adults: int, fallback: str | None) -> str:
    """Build a FlixBus search URL with date / pax pre-filled."""
    if fallback and "flixbus.com" in fallback and "rideDate" in fallback:
        return fallback
    # FlixBus expects dd.mm.yyyy in the search URL.
    d, m, y = date.split("-")[::-1] if "-" in date else (date, "", "")
    ride_date = f"{d}.{m}.{y}" if y else date
    q = _u.urlencode({
        "departureCity": KOSICE_CITY_ID,
        "arrivalCity": WARSAW_CITY_ID,
        "rideDate": ride_date,
        "adult": adults,
        "_locale": "en",
    })
    return f"https://shop.global.flixbus.com/search?{q}"


def _normalise(raw: dict[str, Any], date: str, idx: int, offset: int, adults: int) -> dict[str, Any]:
    return {
        "trip_id": _trip_id(date, idx),
        "type": "flixbus",
        "carrier": "FlixBus",
        "price_eur_per_person": raw.get("price_eur"),
        "duration_min": raw.get("duration_min"),
        "departure": raw.get("departure"),
        "arrival": raw.get("arrival"),
        "stops": raw.get("stops", 0),
        "booking_link": _deep_link(date, adults, raw.get("booking_link")),
        "date": date,
        "date_offset_days": offset,
    }


def _query(date: str) -> list[dict[str, Any]]:
    """Query FlixBus for one date. Always uses adult=1 so the returned
    price.total is a stable per-person quote; group size is handled by the
    booking deep link, not the price."""
    import requests

    key = os.environ["RAPIDAPI_KEY"]
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": RAPIDAPI_HOST}
    try:
        resp = requests.get(
            SEARCH_URL, headers=headers,
            params={
                "from_city_id": KOSICE_CITY_ID,
                "to_city_id": WARSAW_CITY_ID,
                "departure_date": date,
                "adult": "1",
                "currency": "EUR",
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return []

    trips = payload.get("trips", []) or payload.get("data", [])
    out = []
    for t in trips:
        price = (t.get("price") or {}).get("total") or t.get("price_eur")
        out.append({
            "price_eur": price,
            "duration_min": (
                t.get("duration", {}).get("minutes")
                if isinstance(t.get("duration"), dict)
                else t.get("duration_min")
            ),
            "departure": t.get("departure"),
            "arrival": t.get("arrival"),
            "stops": len(t.get("transfers", [])) if t.get("transfers") else 0,
            "booking_link": t.get("url"),
        })
    return out


def _fetch_live(config: dict[str, Any]) -> list[dict[str, Any]]:
    base_date = dt.date.fromisoformat(config["trip"]["dates"]["outbound"])
    link_adults = config["trip"]["group_size"]  # only used in the booking URL
    # Try the configured date first; widen by one day and return at the
    # first offset that yields anything (saves API calls vs accumulating).
    for offset in (0, -1, 1):
        date_iso = (base_date + dt.timedelta(days=offset)).isoformat()
        raw_items = _query(date_iso)
        batch = [
            _normalise(raw, date_iso, idx, offset, link_adults)
            for idx, raw in enumerate(raw_items)
        ]
        if batch:
            return batch
    return []


def _mock_data(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic sample used by --dry-run ONLY."""
    date = config["trip"]["dates"]["outbound"]
    adults = config["trip"]["group_size"]
    samples = [{
        "price_eur": 22, "duration_min": 480, "departure": "07:00",
        "arrival": "15:00", "stops": 1, "booking_link": None,
    }]
    return [_normalise(s, date, i, 0, adults) for i, s in enumerate(samples)]


def fetch_flixbus(config: dict[str, Any], dry_run: bool = False) -> list[dict[str, Any]]:
    if dry_run:
        return _mock_data(config)
    if not os.environ.get("RAPIDAPI_KEY"):
        print("[flixbus] RAPIDAPI_KEY not set — returning empty.")
        return []
    return _fetch_live(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch FlixBus options")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import json

    print(json.dumps(fetch_flixbus(load_config(), dry_run=args.dry_run), indent=2, ensure_ascii=False))

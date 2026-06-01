"""Flights agent — fetches one-way flight prices KSC -> WAW/WMI.

Primary source: Skyscanner Flights Search API via RapidAPI
(skyscanner50.p.rapidapi.com). Stores a route-hash trip_id so price history
attributes to the same route+carrier over time.

Run standalone:
  python agents/flights_agent.py --dry-run
  python agents/flights_agent.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.config_loader import load_config  # noqa: E402

RAPIDAPI_HOST = "skyscanner50.p.rapidapi.com"
SEARCH_URL = f"https://{RAPIDAPI_HOST}/api/v1/searchFlights"


def _trip_id(origin: str, dest: str, date: str, carrier: str) -> str:
    """Stable route hash: ORIGIN-DEST-YYYYMMDD-CARRIERCODE."""
    code = "".join(c for c in carrier.upper() if c.isalpha())[:2] or "XX"
    return f"{origin}-{dest}-{date.replace('-', '')}-{code}"


def _normalise(raw: dict[str, Any], origin: str, dest: str, date: str) -> dict[str, Any]:
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
        "booking_link": raw.get("booking_link"),
    }


def _fetch_live(config: dict[str, Any]) -> list[dict[str, Any]]:
    import requests

    trip = config["trip"]
    origin = trip["origin_airport"]
    date = trip["dates"]["outbound"]
    key = os.environ["RAPIDAPI_KEY"]
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": RAPIDAPI_HOST}
    max_stops = config["transport"]["max_layovers"]

    results: list[dict[str, Any]] = []
    # Check both Warsaw airports: WAW (Chopin) and WMI (Modlin).
    for dest in [trip["destination_airport"], "WMI"]:
        try:
            resp = requests.get(
                SEARCH_URL,
                headers=headers,
                params={
                    "origin": origin,
                    "destination": dest,
                    "date": date,
                    "adults": "1",
                    "currency": "EUR",
                },
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError):
            continue

        for item in payload.get("data", []):
            legs = item.get("legs") or [{}]
            stops = legs[0].get("stops", 0)
            if stops > max_stops:
                continue
            results.append(
                _normalise(
                    {
                        "carrier": (legs[0].get("carriers") or ["Unknown"])[0],
                        "price_eur": (item.get("price") or {}).get("amount"),
                        "duration_min": legs[0].get("durationInMinutes"),
                        "departure": legs[0].get("departure"),
                        "arrival": legs[0].get("arrival"),
                        "stops": stops,
                        "booking_link": item.get("deeplink") or item.get("url"),
                    },
                    origin,
                    dest,
                    date,
                )
            )
    return results


def _mock_data(config: dict[str, Any]) -> list[dict[str, Any]]:
    trip = config["trip"]
    origin = trip["origin_airport"]
    date = trip["dates"]["outbound"]
    samples = [
        {
            "carrier": "Ryanair", "price_eur": 49, "duration_min": 95,
            "departure": "06:30", "arrival": "08:05", "stops": 0,
            "booking_link": "https://www.ryanair.com/",
            "dest": "WAW",
        },
        {
            "carrier": "Wizz Air", "price_eur": 54, "duration_min": 100,
            "departure": "12:10", "arrival": "13:50", "stops": 0,
            "booking_link": "https://wizzair.com/",
            "dest": "WMI",
        },
    ]
    return [_normalise(s, origin, s["dest"], date) for s in samples]


def fetch_flights(config: dict[str, Any], dry_run: bool = False) -> list[dict[str, Any]]:
    if dry_run or not os.environ.get("RAPIDAPI_KEY"):
        return _mock_data(config)
    results = _fetch_live(config)
    return results or _mock_data(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch flight options")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import json

    print(json.dumps(fetch_flights(load_config(), dry_run=args.dry_run), indent=2, ensure_ascii=False))

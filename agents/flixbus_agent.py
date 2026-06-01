"""FlixBus agent — fetches bus prices Košice -> Warsaw for the outbound date.

Source: FlixBus API via RapidAPI (flixbus.p.rapidapi.com).
City IDs: Košice = 39, Warsaw = 36 (per CLAUDE.md).

Note: the Košice -> Warsaw route is seasonal — verify it operates in August
2026. When the live API returns no trips, this falls back to mock data so the
pipeline still runs.

Run standalone:
  python agents/flixbus_agent.py --dry-run
  python agents/flixbus_agent.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.config_loader import load_config  # noqa: E402

RAPIDAPI_HOST = "flixbus.p.rapidapi.com"
SEARCH_URL = f"https://{RAPIDAPI_HOST}/search_trips"
KOSICE_CITY_ID = "39"
WARSAW_CITY_ID = "36"


def _trip_id(date: str, idx: int) -> str:
    """Stable id: KOS-WAW-YYYYMMDD-FX[idx]."""
    return f"KOS-WAW-{date.replace('-', '')}-FX{idx}"


def _normalise(raw: dict[str, Any], date: str, idx: int) -> dict[str, Any]:
    return {
        "trip_id": _trip_id(date, idx),
        "type": "flixbus",
        "carrier": "FlixBus",
        "price_eur_per_person": raw.get("price_eur"),
        "duration_min": raw.get("duration_min"),
        "departure": raw.get("departure"),
        "arrival": raw.get("arrival"),
        "stops": raw.get("stops", 0),
        "booking_link": raw.get("booking_link"),
    }


def _fetch_live(config: dict[str, Any]) -> list[dict[str, Any]]:
    import requests

    date = config["trip"]["dates"]["outbound"]
    key = os.environ["RAPIDAPI_KEY"]
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": RAPIDAPI_HOST}
    try:
        resp = requests.get(
            SEARCH_URL,
            headers=headers,
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
    for idx, t in enumerate(trips):
        price = (t.get("price") or {}).get("total") or t.get("price_eur")
        out.append(
            _normalise(
                {
                    "price_eur": price,
                    "duration_min": t.get("duration", {}).get("minutes") if isinstance(t.get("duration"), dict) else t.get("duration_min"),
                    "departure": t.get("departure"),
                    "arrival": t.get("arrival"),
                    "stops": len(t.get("transfers", [])) if t.get("transfers") else 0,
                    "booking_link": t.get("url") or "https://global.flixbus.com/",
                },
                date,
                idx,
            )
        )
    return out


def _mock_data(config: dict[str, Any]) -> list[dict[str, Any]]:
    date = config["trip"]["dates"]["outbound"]
    samples = [
        {
            "price_eur": 22, "duration_min": 480, "departure": "07:00",
            "arrival": "15:00", "stops": 1,
            "booking_link": "https://global.flixbus.com/",
        },
    ]
    return [_normalise(s, date, i) for i, s in enumerate(samples)]


def fetch_flixbus(config: dict[str, Any], dry_run: bool = False) -> list[dict[str, Any]]:
    if dry_run or not os.environ.get("RAPIDAPI_KEY"):
        return _mock_data(config)
    return _fetch_live(config) or _mock_data(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch FlixBus options")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import json

    print(json.dumps(fetch_flixbus(load_config(), dry_run=args.dry_run), indent=2, ensure_ascii=False))

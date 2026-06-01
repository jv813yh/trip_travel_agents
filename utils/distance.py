"""Distance from accommodation coordinates to the target address.

Uses the Google Maps Distance Matrix API. Distances don't change, so results
are cached on disk (utils/.distance_cache.json) keyed by rounded coordinates —
sheets_writer also persists distance per hotel_id, but this cache avoids
re-billing within and across local runs.

If GOOGLE_MAPS_API_KEY is unset, falls back to the haversine great-circle
distance so the pipeline still produces a usable (approximate) number.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import requests

_CACHE_PATH = Path(__file__).resolve().parent / ".distance_cache.json"
_DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"


def _load_cache() -> dict[str, float]:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict[str, float]) -> None:
    try:
        _CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass  # cache is best-effort; never fail the run over it


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two coordinates."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return round(r * 2 * math.asin(math.sqrt(a)), 2)


def distance_km(
    origin_lat: float,
    origin_lng: float,
    config: dict[str, Any],
    mode: str = "walking",
) -> float:
    """Distance in km from (origin_lat, origin_lng) to the target address.

    The target is config['accommodation']['target_coordinates'] if present,
    otherwise the geocoded 'target_address' (resolved by the Maps API).
    """
    acc = config["accommodation"]
    target = acc.get("target_coordinates") or {}
    target_lat, target_lng = target.get("lat"), target.get("lng")

    cache_key = f"{round(origin_lat, 5)},{round(origin_lng, 5)}|{mode}"
    cache = _load_cache()
    if cache_key in cache:
        return cache[cache_key]

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    result: float | None = None

    if api_key:
        destination = (
            f"{target_lat},{target_lng}"
            if target_lat is not None and target_lng is not None
            else acc["target_address"]
        )
        try:
            resp = requests.get(
                _DISTANCE_MATRIX_URL,
                params={
                    "origins": f"{origin_lat},{origin_lng}",
                    "destinations": destination,
                    "mode": mode,
                    "units": "metric",
                    "key": api_key,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            element = data["rows"][0]["elements"][0]
            if element.get("status") == "OK":
                result = round(element["distance"]["value"] / 1000, 2)
        except (requests.RequestException, KeyError, IndexError, ValueError):
            result = None  # fall through to haversine

    if result is None and target_lat is not None and target_lng is not None:
        result = haversine_km(origin_lat, origin_lng, target_lat, target_lng)

    if result is not None:
        cache[cache_key] = result
        _save_cache(cache)

    return result if result is not None else float("nan")

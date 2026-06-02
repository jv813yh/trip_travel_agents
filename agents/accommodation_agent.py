"""Accommodation agent — fetches stays near the target address.

Uses Apify maintained actors (not raw scraping):
  - Booking.com : voyager/booking-scraper
  - Airbnb      : tri_angle/airbnb-scraper

Each result is normalised to a common dict, distance from the target address is
computed, and a composite score is attached. Stable hotel_id / listing_id values
are preserved so price history attributes to the same property over time.

Run standalone:
  python agents/accommodation_agent.py --dry-run    # mock data, no API calls
  python agents/accommodation_agent.py              # live Apify calls
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

# Allow `python agents/accommodation_agent.py` to import the utils/ package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import urllib.parse as _u  # noqa: E402

from utils.config_loader import load_config  # noqa: E402
from utils.distance import distance_km  # noqa: E402
from utils.scorer import composite_score  # noqa: E402

# Module-level warning bucket — drained by the orchestrator via get_last_errors()
# and surfaced in the daily email when a fallback path was used.
_LAST_ERRORS: list[str] = []


def get_last_errors() -> list[str]:
    """Return + clear warnings recorded by the most recent fetch."""
    out = list(_LAST_ERRORS)
    _LAST_ERRORS.clear()
    return out


def _merge_query(url: str, overrides: dict[str, Any]) -> str:
    """Return `url` with `overrides` merged into its query string.

    Keys in `overrides` REPLACE any existing values; other params are kept.
    Prevents `?checkin=A&checkin=B` when the source URL already carries
    these keys (Apify's Booking actor sometimes does).
    """
    parts = _u.urlsplit(url)
    kept = [(k, v) for k, v in _u.parse_qsl(parts.query, keep_blank_values=True)
            if k not in overrides]
    merged = kept + [(k, str(v)) for k, v in overrides.items()]
    return _u.urlunsplit((parts.scheme, parts.netloc, parts.path,
                          _u.urlencode(merged), parts.fragment))


def _booking_deep_link(url: str | None, config: dict[str, Any]) -> str | None:
    """Set checkin/checkout/group params on a Booking.com hotel URL."""
    if not url or "booking.com" not in url:
        return url
    trip = config["trip"]
    return _merge_query(url, {
        "checkin": trip["dates"]["outbound"],
        "checkout": trip["dates"]["return"],
        "group_adults": trip["group_size"],
        "no_rooms": 1,
        "group_children": 0,
    })


def _airbnb_deep_link(url: str | None, config: dict[str, Any]) -> str | None:
    """Set checkin/checkout/guests params on an Airbnb listing URL."""
    if not url or "airbnb." not in url:
        return url
    trip = config["trip"]
    return _merge_query(url, {
        "check_in": trip["dates"]["outbound"],
        "check_out": trip["dates"]["return"],
        "adults": trip["group_size"],
    })

BOOKING_ACTOR = "voyager/booking-scraper"
AIRBNB_ACTOR = "tri_angle/airbnb-scraper"

# Sky-Scrapper hotels — resolved once via /hotels/searchDestinationOrHotel.
# Refresh if the city changes.
SKYSCANNER_HOST = "sky-scrapper.p.rapidapi.com"
SKYSCANNER_WARSAW_ENTITY_ID = "27547454"


def _normalise(raw: dict[str, Any], source: str, config: dict[str, Any]) -> dict[str, Any]:
    """Map a raw actor item onto the common accommodation schema + score it."""
    lat = _coerce_float(raw.get("lat"))
    lng = _coerce_float(raw.get("lng"))
    dist = distance_km(lat, lng, config) if lat is not None and lng is not None else None
    price = raw.get("price_eur")
    rating = raw.get("rating")

    record = {
        "hotel_id": raw["hotel_id"],
        "source": source,
        "name": raw.get("name"),
        "price_eur": price,
        "rating": rating,
        "lat": lat,
        "lng": lng,
        "distance_km": dist,
        "availability": raw.get("availability", True),
        "booking_link": raw.get("booking_link"),
    }
    record["composite_score"] = (
        composite_score(price, rating, dist, config, source)
        if price is not None
        else None
    )
    return record


def _run_actor(client: Any, actor_id: str, run_input: dict[str, Any]) -> list[dict[str, Any]]:
    run = client.actor(actor_id).call(run_input=run_input)
    dataset_id = run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id
    items = client.dataset(dataset_id).list_items().items
    return items


def _fetch_booking(client: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Call the Booking.com actor and map items to the common schema.

    The actor's raw field names vary; adjust the mapping here if the actor's
    output schema changes.
    """
    trip = config["trip"]
    run_input = {
        "search": trip["destination_city"],
        "checkIn": trip["dates"]["outbound"],
        "checkOut": trip["dates"]["return"],
        "currency": "EUR",
        "adults": trip["group_size"],
        "maxItems": 10,
    }
    items = _run_actor(client, BOOKING_ACTOR, run_input)
    out = []
    for it in items:
        out.append(
            _normalise(
                {
                    "hotel_id": f"bk_{it.get('hotelId') or it.get('id')}",
                    "name": it.get("name"),
                    "price_eur": _to_per_night(_coerce_price(it.get("price")), config),
                    "rating": it.get("rating"),
                    "lat": (it.get("location") or {}).get("lat") or it.get("lat"),
                    "lng": (it.get("location") or {}).get("lng") or it.get("lng"),
                    "availability": True,
                    "booking_link": _booking_deep_link(it.get("url"), config),
                },
                "booking_com",
                config,
            )
        )
    return out


def _skyscanner_hotel_link(hotel_id: str, config: dict[str, Any]) -> str:
    """Build a Skyscanner hotel detail URL with dates + pax pre-filled."""
    trip = config["trip"]
    q = _u.urlencode({
        "entity_id": SKYSCANNER_WARSAW_ENTITY_ID,
        "checkin": trip["dates"]["outbound"],
        "checkout": trip["dates"]["return"],
        "adults": trip["group_size"],
        "rooms": 1,
    })
    return f"https://www.skyscanner.com/hotels/hotel/{hotel_id}?{q}"


def _fetch_skyscanner_hotels(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Sky-Scrapper hotels search → common schema.

    Aggregates Booking/Hotels.com/Expedia; `rawPrice` is per-night EUR (we
    verified: priceDescription matches rawPrice × nights). Uses RapidAPI free
    tier — does NOT count against Apify quota.
    """
    import requests

    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        print("[skyscanner_hotels] RAPIDAPI_KEY not set — skipping.")
        return []

    trip = config["trip"]
    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": SKYSCANNER_HOST,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.get(
            f"https://{SKYSCANNER_HOST}/api/v1/hotels/searchHotels",
            headers=headers,
            params={
                "entityId": SKYSCANNER_WARSAW_ENTITY_ID,
                "checkin": trip["dates"]["outbound"],
                "checkout": trip["dates"]["return"],
                "adults": str(trip["group_size"]),
                "rooms": "1",
                "currency": "EUR",
                "sorting": "-relevance",
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            print(f"[skyscanner_hotels] HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[skyscanner_hotels] call failed: {type(e).__name__}: {e}")
        return []

    hotels = (payload.get("data") or {}).get("hotels", [])
    out = []
    for h in hotels:
        coords = h.get("coordinates") or [None, None]
        # Sky-Scrapper returns [lng, lat] — note the order.
        lng, lat = (coords[0], coords[1]) if len(coords) == 2 else (None, None)
        rating_obj = h.get("rating") or {}
        try:
            rating = float(rating_obj.get("value")) if rating_obj.get("value") else None
        except (TypeError, ValueError):
            rating = None
        out.append(
            _normalise(
                {
                    "hotel_id": f"sk_{h.get('hotelId')}",
                    "name": h.get("name"),
                    "price_eur": _coerce_float(h.get("rawPrice")),
                    "rating": rating,
                    "lat": lat,
                    "lng": lng,
                    "availability": True,
                    "booking_link": _skyscanner_hotel_link(str(h.get("hotelId")), config),
                },
                "skyscanner",
                config,
            )
        )
    return out


def _fetch_airbnb(client: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    trip = config["trip"]
    run_input = {
        "locationQuery": trip["destination_city"],
        "checkIn": trip["dates"]["outbound"],
        "checkOut": trip["dates"]["return"],
        "currency": "EUR",
        "adults": trip["group_size"],
        "maxItems": 10,
    }
    items = _run_actor(client, AIRBNB_ACTOR, run_input)
    out = []
    for it in items:
        out.append(
            _normalise(
                {
                    "hotel_id": f"ab_{it.get('id')}",
                    "name": it.get("name") or it.get("title"),
                    "price_eur": _to_per_night(
                        _coerce_price(it.get("price") or it.get("pricing")), config
                    ),
                    "rating": it.get("rating") or it.get("stars"),
                    "lat": it.get("lat") or (it.get("coordinates") or {}).get("latitude"),
                    "lng": it.get("lng") or (it.get("coordinates") or {}).get("longitude"),
                    "availability": True,
                    "booking_link": _airbnb_deep_link(it.get("url"), config),
                },
                "airbnb",
                config,
            )
        )
    return out


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_price(value: Any) -> float | None:
    """Best-effort extraction of a numeric EUR price (raw, undecided units)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        # Per-night keys win — they are unambiguous.
        for key in ("perNight", "pricePerNight", "price_per_night", "nightly"):
            if key in value:
                return _coerce_price(value[key])
        for key in ("amount", "value", "total", "grossPrice"):
            if key in value:
                return _coerce_price(value[key])
        return None
    # strings like "€72" or "72.00 EUR"
    digits = "".join(c for c in str(value) if c.isdigit() or c == ".")
    return float(digits) if digits else None


def _to_per_night(raw_price: float | None, config: dict[str, Any]) -> float | None:
    """Convert a raw actor price to per-night EUR.

    Booking/Airbnb actors sometimes return a stay-total in the same field they
    elsewhere use for nightly rates. Heuristic: if the value is implausibly
    large for a nightly rate (>2× the configured per-night budget), assume it
    is a stay-total for `accommodation.nights` nights and divide. This makes
    the conversion explicit (the critic flagged silent reinterpretation).
    """
    if raw_price is None:
        return None
    acc = config["accommodation"]
    budget = acc["max_price_per_night_eur"]
    nights = max(1, int(acc.get("nights", 1)))
    if raw_price > budget * 2 and nights > 1:
        return round(raw_price / nights, 2)
    return float(raw_price)


def _mock_data(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic sample used by --dry-run and when APIFY_TOKEN is missing."""
    raw = [
        {
            "hotel_id": "bk_123456", "name": "Apartmán Centrum Warsaw",
            "price_eur": 72, "rating": 9.1, "lat": 52.2290, "lng": 21.0120,
            "booking_link": "https://www.booking.com/hotel/pl/apartman-centrum.html",
        },
        {
            "hotel_id": "ab_789012", "name": "Cozy Studio Śródmieście",
            "price_eur": 65, "rating": 4.87, "lat": 52.2310, "lng": 21.0090,
            "booking_link": "https://www.airbnb.com/rooms/789012",
        },
        {
            "hotel_id": "bk_654321", "name": "Hotel Marszałkowska 18",
            "price_eur": 88, "rating": 8.7, "lat": 52.2250, "lng": 21.0150,
            "booking_link": "https://www.booking.com/hotel/pl/marszalkowska-18.html",
        },
    ]
    return [
        _normalise(r, "airbnb" if r["hotel_id"].startswith("ab_") else "booking_com", config)
        for r in raw
    ]


def fetch_accommodation(config: dict[str, Any], dry_run: bool = False) -> list[dict[str, Any]]:
    """Return scored, filtered accommodation options for the trip dates.

    Source policy: Apify (Booking / Airbnb) is the PRIMARY data source —
    inventory matches our budget apartments better. Sky-Scrapper hotels is a
    FALLBACK, used only when Apify is unavailable (no token, actor failure, or
    zero results — e.g. monthly compute quota depleted). This keeps daily
    API spend on a single platform when both are healthy.
    """
    if dry_run:
        results = _mock_data(config)
    else:
        results = []
        sources = config["accommodation"].get("sources", [])
        token = os.environ.get("APIFY_TOKEN")
        apify_attempted = False
        apify_failed = False

        # ---- Primary: Apify-backed sources ----
        if token and ("booking_com" in sources or "airbnb" in sources):
            apify_attempted = True
            try:
                from apify_client import ApifyClient

                client = ApifyClient(token)
                if "booking_com" in sources:
                    results += _fetch_booking(client, config)
                if "airbnb" in sources:
                    results += _fetch_airbnb(client, config)
            except Exception as e:  # noqa: BLE001 — surface to email, then fall back
                apify_failed = True
                msg = f"Apify call failed ({type(e).__name__}: {e}) — falling back to Skyscanner."
                print(f"[accommodation] {msg}")
                _LAST_ERRORS.append(msg)
        elif "booking_com" in sources or "airbnb" in sources:
            msg = "APIFY_TOKEN missing — falling back to Skyscanner hotels."
            print(f"[accommodation] {msg}")
            _LAST_ERRORS.append(msg)

        # ---- Fallback: Sky-Scrapper hotels ----
        # Trigger when (a) skyscanner is listed AND
        #   (b) Apify wasn't attempted (no token, or no Apify sources listed),
        #       OR Apify threw,
        #       OR Apify returned zero rows (likely quota depleted).
        if "skyscanner" in sources:
            apify_empty = apify_attempted and not apify_failed and not results
            if (not apify_attempted) or apify_failed or apify_empty:
                if apify_empty:
                    msg = "Apify returned 0 results (quota likely depleted) — using Skyscanner fallback."
                    print(f"[accommodation] {msg}")
                    _LAST_ERRORS.append(msg)
                results += _fetch_skyscanner_hotels(config)

        # Fall back to mock if every configured source returned nothing AND no live
        # credentials exist — preserves the old "first run on a laptop" UX.
        if not results and not token and not os.environ.get("RAPIDAPI_KEY"):
            results = _mock_data(config)

    # Drop options outside the hard distance limit; keep everything else for trend data.
    max_dist = config["accommodation"]["max_distance_km"]
    return [
        r for r in results
        if r.get("distance_km") is None or r["distance_km"] <= max_dist * 1.5
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch accommodation options")
    parser.add_argument("--dry-run", action="store_true", help="use mock data, no API calls")
    args = parser.parse_args()

    cfg = load_config()
    data = fetch_accommodation(cfg, dry_run=args.dry_run)
    import json

    print(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\n{len(data)} option(s) within distance limit.", file=sys.stderr)

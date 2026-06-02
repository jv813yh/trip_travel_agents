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
import datetime as dt
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
    merged = kept + [(k, str(v)) for k, v in overrides.items() if v is not None]
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
        "no_rooms": None,
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
AIRBNB_ACTOR = "trakk/airbnb-scraper"

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
    rating = _coerce_rating(raw.get("rating"))

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
        "rooms": raw.get("rooms"),
        "total_group_cost_eur": raw.get("total_group_cost_eur"),
        "price_basis": raw.get("price_basis"),
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
        rooms = _infer_room_count(it)
        price_fields = _normalise_accommodation_price(
            _coerce_price(it.get("price")),
            config,
            rooms=rooms,
            price_covers_all_rooms=True,
        )
        out.append(
            _normalise(
                {
                    "hotel_id": f"bk_{it.get('hotelId') or it.get('id')}",
                    "name": it.get("name"),
                    **price_fields,
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
        msg = "RAPIDAPI_KEY not set — skipping Skyscanner hotels."
        print(f"[skyscanner_hotels] {msg}")
        _LAST_ERRORS.append(msg)
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
                "currency": "EUR",
                "sorting": "-relevance",
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            msg = f"Skyscanner hotels HTTP {resp.status_code}: {resp.text[:200]}"
            print(f"[skyscanner_hotels] {msg}")
            _LAST_ERRORS.append(msg)
            return []
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        msg = f"Skyscanner hotels call failed: {type(e).__name__}: {e}"
        print(f"[skyscanner_hotels] {msg}")
        _LAST_ERRORS.append(msg)
        return []

    hotels = (payload.get("data") or {}).get("hotels", [])
    out = []
    for h in hotels:
        rooms = _infer_room_count(h)
        price_fields = _normalise_accommodation_price(
            _coerce_float(h.get("rawPrice")),
            config,
            rooms=rooms,
            price_covers_all_rooms=True,
        )
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
                    **price_fields,
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
    acc = config["accommodation"]
    run_input = {
        "mode": "search-fast",
        "locationQueries": [acc.get("target_address") or trip["destination_city"]],
        "checkIn": trip["dates"]["outbound"],
        "checkOut": trip["dates"]["return"],
        "currency": "EUR",
        "adults": trip["group_size"],
        "maxPrice": int(acc["max_price_per_night_eur"] * 2),
        "minBeds": trip["group_size"],
        "roomType": "Entire home/apt",
        "maxItemsPerQuery": 30,
        "locale": "en",
    }
    items = _run_actor(client, AIRBNB_ACTOR, run_input)
    out = []
    for it in items:
        coords = it.get("coordinates") or {}
        price = it.get("price") or it.get("pricing")
        price_fields = _normalise_accommodation_price(
            _coerce_price(price),
            config,
            rooms=1,
            price_covers_all_rooms=True,
        )
        if isinstance(coords, list) and len(coords) == 2:
            coords = {"lng": coords[0], "lat": coords[1]}
        out.append(
            _normalise(
                {
                    "hotel_id": f"ab_{it.get('id')}",
                    "name": it.get("name") or it.get("title"),
                    **price_fields,
                    "rating": it.get("rating") or it.get("stars"),
                    "lat": it.get("lat") or coords.get("latitude") or coords.get("lat"),
                    "lng": it.get("lng") or coords.get("longitude") or coords.get("lng"),
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


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(float(str(value).split()[0]))
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _coerce_rating(value: Any) -> float | None:
    """Extract a numeric rating from provider-specific rating objects."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("value", "rating", "score", "guestSatisfactionOverall"):
            if key in value:
                parsed = _coerce_rating(value[key])
                if parsed is not None:
                    return parsed
        for item in value.values():
            parsed = _coerce_rating(item)
            if parsed is not None:
                return parsed
        return None
    try:
        return float(str(value).split()[0])
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
        for key in ("perNight", "pricePerNight", "price_per_night", "nightly", "unit", "rate"):
            if key in value:
                return _coerce_price(value[key])
        for key in ("amount", "value", "total", "grossPrice", "price", "primary"):
            if key in value:
                return _coerce_price(value[key])
        for item in value.values():
            parsed = _coerce_price(item)
            if parsed is not None:
                return parsed
        return None
    # strings like "€72" or "72.00 EUR"
    digits = "".join(c for c in str(value) if c.isdigit() or c == ".")
    return float(digits) if digits else None


def _infer_room_count(value: Any) -> int | None:
    """Best-effort room/unit count from provider-specific result fields."""
    if not isinstance(value, dict):
        return None
    for key in (
        "rooms", "roomCount", "room_count", "noRooms", "no_rooms",
        "numberOfRooms", "selectedRooms", "bedrooms", "bedroomCount",
    ):
        if key in value:
            parsed = _coerce_int(value[key])
            if parsed is not None:
                return parsed
    for key in ("room", "roomInfo", "unit", "selectedRoom", "accommodation"):
        nested = value.get(key)
        if isinstance(nested, dict):
            parsed = _infer_room_count(nested)
            if parsed is not None:
                return parsed
    for key in ("rooms", "roomTypes", "blocks", "units"):
        nested_list = value.get(key)
        if isinstance(nested_list, list) and nested_list:
            return len(nested_list)
    return None


def _stay_nights(config: dict[str, Any]) -> int:
    """Return nights from trip dates, falling back to config if parsing fails."""
    try:
        dates = config["trip"]["dates"]
        outbound = dt.date.fromisoformat(dates["outbound"])
        return_date = dt.date.fromisoformat(dates["return"])
        return max(1, (return_date - outbound).days)
    except (KeyError, TypeError, ValueError):
        return max(1, int(config["accommodation"].get("nights", 1)))


def _normalise_accommodation_price(
    raw_price: float | None,
    config: dict[str, Any],
    *,
    rooms: int | None,
    price_covers_all_rooms: bool = False,
) -> dict[str, Any]:
    """Convert a provider price to EUR per person per night.

    Providers may return either a nightly price or a whole-stay total. For
    Hotel-style sources are queried for the configured group size, so their
    observed price should cover the returned offer. If a source only returns a
    one-room price and also tells us the room count, pass price_covers_all_rooms=False.
    """
    basis = "eur_per_person_per_night"
    if raw_price is None:
        return {
            "price_eur": None,
            "rooms": rooms,
            "total_group_cost_eur": None,
            "price_basis": basis,
        }

    acc = config["accommodation"]
    budget = acc["max_price_per_night_eur"]
    nights = _stay_nights(config)
    group_size = max(1, int(config["trip"]["group_size"]))
    rooms = _coerce_int(rooms)
    room_multiplier = 1 if price_covers_all_rooms else (rooms or 1)
    is_stay_total = raw_price > budget * 2 and nights > 1

    if is_stay_total:
        per_person_per_night = raw_price * room_multiplier / group_size / nights
    else:
        per_person_per_night = raw_price * room_multiplier / group_size

    total_group_cost = per_person_per_night * group_size * nights
    return {
        "price_eur": round(per_person_per_night, 2),
        "rooms": rooms,
        "total_group_cost_eur": round(total_group_cost, 2),
        "price_basis": basis,
    }


def _mock_data(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic sample used by --dry-run and when APIFY_TOKEN is missing."""
    group_size = max(1, int(config["trip"]["group_size"]))
    nights = _stay_nights(config)
    raw = [
        {
            "hotel_id": "bk_123456", "name": "Apartmán Centrum Warsaw",
            "price_eur": 72, "rating": 9.1, "lat": 52.2290, "lng": 21.0120,
            "booking_link": _booking_deep_link(
                "https://www.booking.com/hotel/pl/apartman-centrum.html",
                config,
            ),
            "rooms": None,
        },
        {
            "hotel_id": "ab_789012", "name": "Cozy Studio Śródmieście",
            "price_eur": 65, "rating": 4.87, "lat": 52.2310, "lng": 21.0090,
            "booking_link": "https://www.airbnb.com/rooms/789012",
            "rooms": 1,
        },
        {
            "hotel_id": "bk_654321", "name": "Hotel Marszałkowska 18",
            "price_eur": 88, "rating": 8.7, "lat": 52.2250, "lng": 21.0150,
            "booking_link": _booking_deep_link(
                "https://www.booking.com/hotel/pl/marszalkowska-18.html",
                config,
            ),
            "rooms": None,
        },
    ]
    for item in raw:
        item["total_group_cost_eur"] = round(item["price_eur"] * group_size * nights, 2)
        item["price_basis"] = "eur_per_person_per_night"
    return [
        _normalise(r, "airbnb" if r["hotel_id"].startswith("ab_") else "booking_com", config)
        for r in raw
    ]


def _dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort de-dupe across providers by exact id/link/name+distance.

    Same properties can appear through Booking.com and Skyscanner. Keep the
    row with the lower price; break ties by composite score.
    """
    deduped: dict[str, dict[str, Any]] = {}
    for item in results:
        name = (item.get("name") or "").casefold().strip()
        dist = item.get("distance_km")
        dist_bucket = round(float(dist), 1) if dist is not None else "na"
        key = item.get("booking_link") or f"{name}|{dist_bucket}"
        current = deduped.get(key)
        if current is None:
            deduped[key] = item
            continue
        item_price = item.get("price_eur")
        current_price = current.get("price_eur")
        if current_price is None or (
            item_price is not None and item_price < current_price
        ):
            deduped[key] = item
        elif item_price == current_price and (
            (item.get("composite_score") or 0) > (current.get("composite_score") or 0)
        ):
            deduped[key] = item
    return list(deduped.values())


def fetch_accommodation(config: dict[str, Any], dry_run: bool = False) -> list[dict[str, Any]]:
    """Return scored, filtered accommodation options for the trip dates.

    Every configured source is queried. Booking.com and Airbnb use Apify
    actors; Skyscanner hotels uses RapidAPI and broadens the market with hotel
    aggregators. If all live providers are unavailable, local runs without
    credentials still fall back to deterministic mock data.
    """
    if dry_run:
        results = _mock_data(config)
    else:
        results = []
        sources = config["accommodation"].get("sources", [])
        token = os.environ.get("APIFY_TOKEN")

        # ---- Apify-backed sources ----
        if token and ("booking_com" in sources or "airbnb" in sources):
            try:
                from apify_client import ApifyClient

                client = ApifyClient(token)
                if "booking_com" in sources:
                    try:
                        booking_results = _fetch_booking(client, config)
                        if booking_results:
                            results += booking_results
                        else:
                            _LAST_ERRORS.append("Booking.com Apify returned 0 rows.")
                    except Exception as e:  # noqa: BLE001
                        msg = f"Booking.com Apify call failed ({type(e).__name__}: {e})."
                        print(f"[accommodation] {msg}")
                        _LAST_ERRORS.append(msg)
                if "airbnb" in sources:
                    try:
                        airbnb_results = _fetch_airbnb(client, config)
                        if airbnb_results:
                            results += airbnb_results
                        else:
                            _LAST_ERRORS.append("Airbnb Apify returned 0 rows.")
                    except Exception as e:  # noqa: BLE001
                        msg = f"Airbnb Apify call failed ({type(e).__name__}: {e})."
                        print(f"[accommodation] {msg}")
                        _LAST_ERRORS.append(msg)
            except Exception as e:  # noqa: BLE001
                msg = f"Apify client failed ({type(e).__name__}: {e})."
                print(f"[accommodation] {msg}")
                _LAST_ERRORS.append(msg)
        elif "booking_com" in sources or "airbnb" in sources:
            msg = "APIFY_TOKEN missing — skipping Booking.com/Airbnb Apify sources."
            print(f"[accommodation] {msg}")
            _LAST_ERRORS.append(msg)

        # ---- Aggregator source ----
        if "skyscanner" in sources:
            sky_results = _fetch_skyscanner_hotels(config)
            if sky_results:
                results += sky_results
            elif not os.environ.get("RAPIDAPI_KEY"):
                _LAST_ERRORS.append("RAPIDAPI_KEY missing — skipping Skyscanner hotels.")
            else:
                _LAST_ERRORS.append("Skyscanner hotels returned 0 rows.")

        # Fall back to mock if every configured source returned nothing AND no live
        # credentials exist — preserves the old "first run on a laptop" UX.
        if not results and not token and not os.environ.get("RAPIDAPI_KEY"):
            results = _mock_data(config)

    results = _dedupe_results(results)

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

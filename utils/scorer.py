"""Composite score calculator for accommodation options.

Score is 0–100, split: price 40 / rating 35 / distance 25.
See CLAUDE.md "Composite score formula".
"""

from __future__ import annotations

from typing import Any


def normalise_rating(rating: float, source: str) -> float:
    """Normalise a rating to the Booking 0–10 scale.

    Airbnb ratings are on a 0–5 scale; everything else is assumed 0–10.
    """
    if source == "airbnb" and rating is not None and rating <= 5:
        return rating * 2
    return rating


def composite_score(
    price_eur: float,
    rating: float,
    distance_km: float,
    config: dict[str, Any],
    source: str = "booking_com",
) -> float:
    """Return the 0–100 composite score for one accommodation option."""
    acc = config["accommodation"]
    budget = acc["max_price_per_night_eur"]
    min_price = budget * 0.5  # ~50% of budget assumed to be the best realistic price

    # Price score (40 pts): linear, lower price = higher score. Clamped to [0, 40].
    if budget == min_price:
        price_score = 40.0
    else:
        price_score = 40 * (1 - (price_eur - min_price) / (budget - min_price))
    price_score = max(0.0, min(40.0, price_score))

    # Rating score (35 pts): normalise to 0–10 then map onto 0–35.
    rating_10 = normalise_rating(rating, source) if rating is not None else 0
    rating_score = max(0.0, min(35.0, (rating_10 / 10) * 35))

    # Distance score (25 pts): 0 km = 25 pts, >= max_dist km = 0 pts.
    max_dist = acc["max_distance_km"]
    if distance_km is None:
        distance_score = 0.0
    else:
        distance_score = max(0.0, 25 * (1 - distance_km / max_dist))
    distance_score = min(25.0, distance_score)

    return round(price_score + rating_score + distance_score, 1)


if __name__ == "__main__":
    from utils.config_loader import load_config

    cfg = load_config()
    demo = composite_score(72, 9.1, 0.8, cfg, "booking_com")
    print(f"demo composite score (72eur, 9.1, 0.8km) = {demo}")

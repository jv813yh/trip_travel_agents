"""Price tracking & alert logic.

Compares today's accommodation prices against historical data (from the
accommodation_raw sheet) and flags significant drops or budget breaches.
Mutates each option in-place to add vs_yesterday_pct / vs_7d_avg_pct /
alert_triggered, and returns the list of triggered alerts.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def check_price_alerts(
    today_data: list[dict[str, Any]],
    history_df: pd.DataFrame,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Annotate today_data with trend fields and return triggered alerts."""
    alerts: list[dict[str, Any]] = []
    threshold = config["alerts"]["price_drop_threshold_pct"]
    budget = config["accommodation"]["max_price_per_night_eur"]

    history_empty = history_df is None or history_df.empty

    for prop in today_data:
        hid = prop["hotel_id"]
        price_today = prop["price_eur"]
        prop["vs_yesterday_pct"] = None
        prop["vs_7d_avg_pct"] = None
        prop["alert_triggered"] = False

        if price_today is None:
            continue

        hist = (
            pd.DataFrame()
            if history_empty
            else history_df[history_df["hotel_id"] == hid].sort_values("date")
        )

        if len(hist) == 0:
            # First time we've seen this property — no baseline, but a price
            # already under budget is still worth alerting on.
            if config["alerts"].get("budget_breach_alert") and price_today < budget:
                prop["alert_triggered"] = True
                alerts.append(_alert(prop, hid, None, price_today, None, "new_under_budget"))
            continue

        yesterday_price = float(hist.iloc[-1]["price_eur"])
        change_vs_yesterday = (
            (price_today - yesterday_price) / yesterday_price * 100
            if yesterday_price
            else 0.0
        )

        last7 = pd.to_numeric(hist.tail(7)["price_eur"], errors="coerce")
        avg_7d = last7.mean()
        change_vs_7d = (
            (price_today - avg_7d) / avg_7d * 100 if avg_7d else 0.0
        )

        prop["vs_yesterday_pct"] = round(change_vs_yesterday, 1)
        prop["vs_7d_avg_pct"] = round(change_vs_7d, 1)

        budget_breach = config["alerts"].get("budget_breach_alert") and price_today < budget
        if change_vs_yesterday <= -threshold or budget_breach:
            prop["alert_triggered"] = True
            alerts.append(
                _alert(
                    prop,
                    hid,
                    yesterday_price,
                    price_today,
                    change_vs_yesterday,
                    "price_drop" if change_vs_yesterday <= -threshold else "under_budget",
                )
            )

    return alerts


def _alert(
    prop: dict[str, Any],
    hid: str,
    prev_price: float | None,
    new_price: float,
    change_pct: float | None,
    alert_type: str,
) -> dict[str, Any]:
    return {
        "alert_type": alert_type,
        "property": prop.get("name"),
        "hotel_id": hid,
        "prev_price": prev_price,
        "new_price": new_price,
        "change_pct": round(change_pct, 1) if change_pct is not None else None,
        "link": prop.get("booking_link"),
    }

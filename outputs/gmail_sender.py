"""Gmail daily digest sender.

Builds an HTML email from the analyser output: transport recommendation, top-2
accommodation picks, price alerts, and an inline 7-day trend chart (matplotlib,
embedded as a base64 <img>). Sends via the Gmail API using OAuth2 credentials
provided base64-encoded in GMAIL_CREDENTIALS.

Without GMAIL_CREDENTIALS / RECIPIENT_EMAIL, build_html() still works (useful
for previews) but send() becomes a no-op that writes the HTML to outputs/.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import pandas as pd

from utils.scorer import normalise_rating

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
_PREVIEW_PATH = Path(__file__).resolve().parent / "_last_digest.html"


def _trend_chart_b64(history_df: pd.DataFrame, top_picks: list[dict[str, Any]]) -> str | None:
    """Render a 7-day price trend for the top picks, return base64 PNG (or None)."""
    if history_df is None or history_df.empty or not top_picks:
        return None
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6, 2.6), dpi=120)
    plotted = False
    for pick in top_picks:
        hist = history_df[history_df["hotel_id"] == pick["hotel_id"]].copy()
        if hist.empty:
            continue
        hist = hist.sort_values("date").tail(7)
        ax.plot(hist["date"], hist["price_eur"], marker="o", label=pick["name"][:24])
        plotted = True
    if not plotted:
        plt.close(fig)
        return None

    ax.set_ylabel("€ / night")
    ax.set_title("7-day price trend")
    ax.legend(fontsize=7)
    ax.tick_params(axis="x", labelrotation=45, labelsize=7)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _score_breakdown(
    price: float, rating: float, distance_km: float,
    config: dict[str, Any], source: str = "booking_com",
) -> str:
    """Return a one-line explanation of the composite score components.

    `source` is needed so Airbnb's 0–5 ratings are normalised onto the 0–10
    scale before scoring (matches `utils.scorer.composite_score`). Without
    this an Airbnb 4.87/5 would show as "good" instead of "excellent".
    """
    acc_cfg = config["accommodation"]
    budget = acc_cfg["max_price_per_night_eur"]
    max_dist = acc_cfg["max_distance_km"]
    min_price = budget * 0.5

    def label(value: float, ceiling: float, ranges: list[tuple[float, str]]) -> str:
        pct = max(0.0, min(1.0, value / ceiling)) * 100
        for threshold, name in ranges:
            if pct >= threshold:
                return name
        return ranges[-1][1]

    price_pts = max(0, 40 * (1 - (price - min_price) / (budget - min_price))) if price else 0
    rating_10 = normalise_rating(rating, source) if rating else 0
    rating_pts = (rating_10 / 10) * 35 if rating_10 else 0
    dist_pts = max(0, 25 * (1 - (distance_km or 0) / max_dist))

    price_label = label(price_pts, 40, [(75, "great price"), (50, "good price"), (25, "ok price"), (0, "over budget")])
    rating_label = label(rating_pts, 35, [(75, "excellent"), (60, "very good"), (40, "good"), (0, "average")])
    dist_label = label(dist_pts, 25, [(75, "very close"), (50, "close"), (25, "walkable"), (0, "far")])

    return (
        f"€{price:g}/person/night → {price_label} (+{price_pts:.0f}/40) · "
        f"rating {rating:g} → {rating_label} (+{rating_pts:.0f}/35) · "
        f"{distance_km:.2f} km → {dist_label} (+{dist_pts:.0f}/25)"
    )


def _date_badge(offset: int | None) -> str:
    if offset is None or offset == 0:
        return ""
    sign = "+" if offset > 0 else ""
    return (
        f" <span style='background:#fff3cd;color:#856404;padding:2px 6px;"
        f"border-radius:4px;font-size:12px'>{sign}{offset} day alt</span>"
    )


def _transport_link_label(option: dict[str, Any]) -> str:
    """Return a clear label for transport booking/search links."""
    link = option.get("booking_link") or ""
    if option.get("type") == "flight" and "skyscanner." in link:
        return "Search on Skyscanner →"
    if option.get("type") == "flight" and "kiwi.com" in link:
        return "Search on Kiwi →"
    if option.get("type") == "flixbus":
        return "Book on FlixBus →"
    return "Book →"


def _stops_text(option: dict[str, Any]) -> str:
    stops = option.get("stops")
    if option.get("type") != "flight" or stops is None:
        return ""
    if stops == 0:
        return " · direct"
    return f" · {stops} stop{'s' if stops != 1 else ''}, verify itinerary"


def _duration_text(minutes: int | None) -> str:
    if minutes is None:
        return "unknown duration"
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _best_transport(options: list[dict[str, Any]], transport_type: str) -> dict[str, Any] | None:
    typed = [
        o for o in options
        if o.get("type") == transport_type and o.get("price_eur_per_person") is not None
    ]
    if not typed:
        return None
    return min(typed, key=lambda o: (o.get("price_eur_per_person") or 1e9, o.get("duration_min") or 1e9))


def _transport_card(option: dict[str, Any], title: str, recommended: bool = False) -> str:
    badge = (
        " <span style='background:#d1e7dd;color:#0f5132;padding:2px 6px;"
        "border-radius:4px;font-size:12px'>recommended</span>"
        if recommended else ""
    )
    price = option.get("price_eur_per_person")
    total = option.get("total_group_cost_eur")
    return (
        "<div style='border:1px solid #dde3ea;border-radius:6px;padding:10px 12px;"
        "margin:8px 0;background:#fff'>"
        f"<b>{title}: {option.get('carrier')}</b>{badge}{_date_badge(option.get('date_offset_days'))}<br>"
        f"€{price}/person"
        + (f" · group total €{total}" if total is not None else "")
        + f" · {_duration_text(option.get('duration_min'))}"
        + f" · {option.get('departure')}→{option.get('arrival')}{_stops_text(option)}<br>"
        f"<a href='{option.get('booking_link')}'>{_transport_link_label(option)}</a>"
        "</div>"
    )


def build_html(analysis: dict[str, Any], history_df: pd.DataFrame, config: dict[str, Any]) -> str:
    acc = analysis["accommodation"]
    transport = analysis["transport"]
    target = config["accommodation"]["target_address"]
    picks = acc["top_picks"]
    trip = config["trip"]
    transport_warnings = analysis.get("_transport_warnings") or []
    accommodation_warnings = analysis.get("_accommodation_warnings") or []
    spreadsheet_url = analysis.get("_spreadsheet_url") or config.get("sheets", {}).get("spreadsheet_url")

    rec_type = transport.get("recommendation")
    rec = next((o for o in transport["options"] if o["type"] == rec_type), None) if rec_type else None
    best_flight = _best_transport(transport.get("options", []), "flight")
    best_flixbus = _best_transport(transport.get("options", []), "flixbus")

    parts: list[str] = ["<div style='font-family:Arial,sans-serif;max-width:640px'>"]

    # Trip header — always show route + dates + pax so the email is self-contained.
    parts.append(
        "<div style='background:#eef5fb;padding:10px 14px;border-radius:6px;"
        "margin-bottom:12px;font-size:14px'>"
        f"<b>{trip['origin_city']} → {trip['destination_city']}</b> · "
        f"{trip['dates']['outbound']} → {trip['dates']['return']} · "
        f"{trip['group_size']} people"
        + (
            f"<br><a href='{spreadsheet_url}' style='color:#0b57d0'>Open tracking sheet + accommodation stats →</a>"
            if spreadsheet_url else ""
        )
        + "</div>"
    )

    # 0. Critic warning banner (only when invalid)
    critic = analysis.get("_critic") or {}
    if not critic.get("valid", True) and critic.get("issues"):
        parts.append(
            "<div style='background:#f8d7da;color:#721c24;padding:10px 14px;"
            "border-radius:6px;margin-bottom:12px'>"
            "<b>⚠️ Critic flagged this run:</b><ul style='margin:6px 0 0 18px'>"
            + "".join(f"<li>{i}</li>" for i in critic["issues"])
            + "</ul></div>"
        )

    # 1. Transport
    parts.append("<h2>🚆 Transport recommendation</h2>")
    if best_flight or best_flixbus:
        if best_flight:
            parts.append(_transport_card(best_flight, "Best flight", rec_type == "flight"))
        else:
            parts.append(
                "<div style='border:1px solid #f5c2c7;border-radius:6px;padding:10px 12px;"
                "margin:8px 0;background:#fff5f5'><b>Best flight:</b> no flight data found today.</div>"
            )
        if best_flixbus:
            parts.append(_transport_card(best_flixbus, "Best FlixBus", rec_type == "flixbus"))
        else:
            parts.append(
                "<div style='border:1px solid #f5c2c7;border-radius:6px;padding:10px 12px;"
                "margin:8px 0;background:#fff5f5'><b>Best FlixBus:</b> no FlixBus data found today.</div>"
            )
    elif transport_warnings:
        parts.append(
            "<p style='color:#a00'><b>Transport data unavailable</b> — the upstream APIs "
            "rejected our request, so we have no live prices for this run:</p>"
            "<ul style='color:#a00;font-size:13px'>"
            + "".join(f"<li>{w}</li>" for w in transport_warnings)
            + "</ul>"
        )
    else:
        parts.append(
            "<p style='color:#a00'><b>No transport options found</b> for the configured date "
            "(or ±1 day). Consider checking manually or adjusting your travel window.</p>"
        )

    # Additional transport options sorted by price (so the user sees the market
    # without duplicating the main flight / FlixBus comparison cards).
    all_options = sorted(
        [
            o for o in transport.get("options", [])
            if o is not best_flight and o is not best_flixbus
        ],
        key=lambda o: (o.get("price_eur_per_person") or 1e9),
    )
    if all_options:
        parts.append("<details><summary style='cursor:pointer;color:#555;font-size:13px'>"
                     f"Other options ({len(all_options)})</summary><ul style='font-size:13px'>")
        for o in all_options:
            parts.append(
                f"<li><b>{o.get('carrier')}</b> ({o.get('type')})"
                f"{_date_badge(o.get('date_offset_days'))} — "
                f"€{o.get('price_eur_per_person')}/person · "
                f"{o.get('duration_min')} min · "
                f"{o.get('departure')}→{o.get('arrival')}{_stops_text(o)} · "
                f"<a href='{o.get('booking_link')}'>{_transport_link_label(o)}</a></li>"
            )
        parts.append("</ul></details>")

    parts.append(f"<p style='color:#555'>{transport.get('reasoning','')}</p>")

    # 2. Accommodation
    parts.append(f"<h2>🏠 Top picks near {target}</h2>")
    if accommodation_warnings:
        parts.append(
            "<div style='background:#fff3cd;color:#856404;padding:8px 12px;"
            "border-radius:6px;margin-bottom:10px;font-size:13px'>"
            "<b>ℹ️ Source note:</b><ul style='margin:4px 0 0 18px'>"
            + "".join(f"<li>{w}</li>" for w in accommodation_warnings)
            + "</ul></div>"
        )
    parts.append(
        "<p style='color:#777;font-size:12px;margin-top:-8px'>"
        "Match score is out of 100: price per person per night (40 pts) + rating (35 pts) + "
        "distance to your friends (25 pts). Higher is better."
        "</p>"
    )
    for p in picks:
        alert_badge = " 🔔" if p.get("alert_triggered") else ""
        vs_y = p.get("vs_yesterday_pct")
        vs_y_str = f" · {vs_y:+.1f}% vs yesterday" if vs_y is not None else ""
        breakdown = _score_breakdown(
            p.get("price_eur_per_night") or 0,
            p.get("rating") or 0,
            p.get("distance_km") or 0,
            config,
            source=p.get("source", "booking_com"),
        )
        rooms = p.get("rooms")
        total = p.get("total_group_cost_eur")
        room_text = (
            f" · {rooms} room{'s' if rooms != 1 else ''}"
            if rooms else " · room count from provider not confirmed"
        )
        cost_context = (
            f"<br><span style='color:#555;font-size:13px'>Estimated group total: €{total:g}"
            + room_text
            + "</span>"
            if total is not None else ""
        )
        parts.append(
            f"<p><b>#{p['rank']} {p['name']}</b>{alert_badge}<br>"
            f"<b>Match: {p['composite_score']:.0f}/100</b>{vs_y_str}<br>"
            f"<span style='color:#555;font-size:13px'>{breakdown}</span>{cost_context}<br>"
            f"<a href='{p['booking_link']}'>Book →</a></p>"
        )

    # 3. Alerts
    alerts = acc.get("alerts", [])
    if alerts:
        parts.append("<h2>🔔 Price alerts</h2><ul>")
        for a in alerts:
            chg = f"{a['change_pct']:+.1f}%" if a.get("change_pct") is not None else "new"
            parts.append(
                f"<li>{a['property']}: €{a.get('prev_price')}→€{a['new_price']} ({chg})</li>"
            )
        parts.append("</ul>")

    # 4. Trend chart
    chart = _trend_chart_b64(history_df, picks)
    if chart:
        parts.append("<h2>📈 Trend</h2>")
        parts.append(f"<img src='data:image/png;base64,{chart}' alt='7-day price trend'/>")

    parts.append("</div>")
    return "".join(parts)


def _subject(analysis: dict[str, Any]) -> str:
    picks = analysis["accommodation"]["top_picks"]
    top_price = picks[0]["price_eur_per_night"] if picks else "?"
    return f"🏕️ Warsaw Trip Update — {analysis['run_date']} | Top pick: €{top_price}/person/night"


def _gmail_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    info = json.loads(base64.b64decode(os.environ["GMAIL_CREDENTIALS"]))
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    return build("gmail", "v1", credentials=creds)


def send(analysis: dict[str, Any], history_df: pd.DataFrame, config: dict[str, Any]) -> bool:
    """Send the digest. Returns True if actually sent, False if previewed only."""
    html = build_html(analysis, history_df, config)
    recipient = os.environ.get("RECIPIENT_EMAIL")

    if not (os.environ.get("GMAIL_CREDENTIALS") and recipient):
        _PREVIEW_PATH.write_text(html, encoding="utf-8")
        print(f"[gmail] credentials/recipient missing — preview written to {_PREVIEW_PATH}")
        return False

    service = _gmail_service()
    message = MIMEText(html, "html", "utf-8")
    message["to"] = recipient
    message["subject"] = _subject(analysis)
    message["from"] = "me"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"[gmail] digest sent to {recipient}")
    return True

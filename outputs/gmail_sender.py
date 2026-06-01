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


def build_html(analysis: dict[str, Any], history_df: pd.DataFrame, config: dict[str, Any]) -> str:
    acc = analysis["accommodation"]
    transport = analysis["transport"]
    target = config["accommodation"]["target_address"]
    picks = acc["top_picks"]

    rec_type = transport.get("recommendation")
    rec = next((o for o in transport["options"] if o["type"] == rec_type), None)

    parts: list[str] = ["<div style='font-family:Arial,sans-serif;max-width:640px'>"]

    # 1. Transport
    parts.append("<h2>🚆 Transport recommendation</h2>")
    if rec:
        parts.append(
            f"<p><b>{rec['carrier']}</b> ({rec['type']}) — "
            f"€{rec['price_eur_per_person']}/person · {rec.get('duration_min')} min · "
            f"{rec.get('departure')}→{rec.get('arrival')} · "
            f"group total €{rec.get('total_group_cost_eur')}<br>"
            f"<a href='{rec.get('booking_link')}'>Book →</a></p>"
        )
    parts.append(f"<p style='color:#555'>{transport.get('reasoning','')}</p>")

    # 2. Accommodation
    parts.append(f"<h2>🏠 Top picks near {target}</h2>")
    for p in picks:
        alert_badge = " 🔔" if p.get("alert_triggered") else ""
        vs_y = p.get("vs_yesterday_pct")
        vs_y_str = f" · {vs_y:+.1f}% vs yesterday" if vs_y is not None else ""
        parts.append(
            f"<p><b>#{p['rank']} {p['name']}</b>{alert_badge}<br>"
            f"€{p['price_eur_per_night']}/night · rating {p['rating']} · "
            f"{p['distance_km']} km · score {p['composite_score']}{vs_y_str}<br>"
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
    return f"🏕️ Warsaw Trip Update — {analysis['run_date']} | Top pick: €{top_price}/night"


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

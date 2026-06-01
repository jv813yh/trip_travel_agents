"""Google Sheets writer + history reader.

Four worksheets (see CLAUDE.md "Google Sheets structure"):
  accommodation_raw, transport_raw, daily_top2, alerts_log

Auth uses a service-account JSON provided base64-encoded in the
GOOGLE_SHEETS_CREDENTIALS env var. When credentials are absent (dry-run / local
without secrets), all writes become no-ops and read_accommodation_history()
returns an empty frame.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
from typing import Any

import pandas as pd

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WORKSHEETS = {
    "accommodation_raw": [
        "date", "hotel_id", "source", "name", "price_eur", "rating", "lat", "lng",
        "distance_km", "availability", "booking_link", "composite_score",
    ],
    "transport_raw": [
        "date", "trip_id", "type", "carrier", "price_eur_pp", "duration_min",
        "departure", "arrival", "stops", "booking_link",
    ],
    "daily_top2": [
        "date", "rank", "type", "name", "price_eur", "composite_score",
        "vs_yesterday_pct", "vs_7d_avg_pct", "alert_triggered", "link",
    ],
    "alerts_log": [
        "timestamp", "alert_type", "property_name", "prev_price", "new_price",
        "change_pct", "notified_via",
    ],
}


class SheetsWriter:
    """Thin wrapper over gspread. No-ops gracefully without credentials."""

    def __init__(self, config: dict[str, Any], dry_run: bool = False):
        self.config = config
        self.spreadsheet_name = config.get("sheets", {}).get(
            "spreadsheet_name", "Poland Trip Tracker"
        )
        self._spreadsheet = None
        self.enabled = bool(os.environ.get("GOOGLE_SHEETS_CREDENTIALS")) and not dry_run
        if self.enabled:
            self._connect()

    def _connect(self) -> None:
        import gspread
        from google.oauth2.service_account import Credentials

        raw = os.environ["GOOGLE_SHEETS_CREDENTIALS"]
        info = json.loads(base64.b64decode(raw))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        client = gspread.authorize(creds)
        self._spreadsheet = client.open(self.spreadsheet_name)
        self._ensure_worksheets()

    def _ensure_worksheets(self) -> None:
        existing = {ws.title for ws in self._spreadsheet.worksheets()}
        for title, header in WORKSHEETS.items():
            if title not in existing:
                ws = self._spreadsheet.add_worksheet(title=title, rows=1000, cols=len(header))
                ws.append_row(header)

    def _append(self, worksheet: str, rows: list[list[Any]]) -> None:
        if not self.enabled or not rows:
            return
        ws = self._spreadsheet.worksheet(worksheet)
        ws.append_rows(rows, value_input_option="USER_ENTERED")

    # ---- writes ---------------------------------------------------------

    def write_accommodation(self, run_date: str, options: list[dict[str, Any]]) -> None:
        rows = [
            [
                run_date, o["hotel_id"], o["source"], o["name"], o["price_eur"],
                o["rating"], o["lat"], o["lng"], o["distance_km"],
                o.get("availability", True), o["booking_link"], o.get("composite_score"),
            ]
            for o in options
        ]
        self._append("accommodation_raw", rows)

    def write_transport(self, run_date: str, options: list[dict[str, Any]]) -> None:
        rows = [
            [
                run_date, o["trip_id"], o["type"], o["carrier"],
                o["price_eur_per_person"], o["duration_min"], o["departure"],
                o["arrival"], o["stops"], o["booking_link"],
            ]
            for o in options
        ]
        self._append("transport_raw", rows)

    def write_daily_top2(self, run_date: str, analysis: dict[str, Any]) -> None:
        rows = []
        for pick in analysis["accommodation"]["top_picks"]:
            rows.append(
                [
                    run_date, pick["rank"], "accommodation", pick["name"],
                    pick["price_eur_per_night"], pick["composite_score"],
                    pick.get("vs_yesterday_pct"), pick.get("vs_7d_avg_pct"),
                    pick.get("alert_triggered", False), pick["booking_link"],
                ]
            )
        self._append("daily_top2", rows)

    def write_alerts(self, alerts: list[dict[str, Any]], notified_via: str = "email") -> None:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        rows = [
            [
                now, "price_drop", a["property"], a["prev_price"], a["new_price"],
                a["change_pct"], notified_via,
            ]
            for a in alerts
        ]
        self._append("alerts_log", rows)

    # ---- reads ----------------------------------------------------------

    def read_accommodation_history(self) -> pd.DataFrame:
        """Return the accommodation_raw sheet as a DataFrame (empty if disabled)."""
        if not self.enabled:
            return pd.DataFrame(columns=WORKSHEETS["accommodation_raw"])
        ws = self._spreadsheet.worksheet("accommodation_raw")
        records = ws.get_all_records()
        df = pd.DataFrame(records)
        if not df.empty:
            df["price_eur"] = pd.to_numeric(df["price_eur"], errors="coerce")
        return df

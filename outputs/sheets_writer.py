"""Google Sheets writer + history reader + formatter.

Four worksheets (see CLAUDE.md "Google Sheets structure"):
  accommodation_raw, transport_raw, daily_top2, alerts_log

Each worksheet is created with:
  - bold white-on-blue header row (frozen)
  - per-column number/date/currency formats
  - sensible column widths
  - autofilter

Use `SheetsWriter(config).setup(reset=True)` to wipe and re-initialise.

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

# Column definitions: (header, type, width_px)
# type one of: date, datetime, text, link, eur, number, percent, integer, bool
COLUMNS = {
    "accommodation_raw": [
        ("date", "date", 90),
        ("hotel_id", "text", 110),
        ("source", "text", 100),
        ("name", "text", 260),
        ("price_eur", "eur", 95),
        ("rating", "number", 70),
        ("lat", "number", 90),
        ("lng", "number", 90),
        ("distance_km", "number", 100),
        ("availability", "bool", 100),
        ("booking_link", "link", 220),
        ("composite_score", "number", 110),
        ("rooms", "integer", 70),
        ("total_group_cost_eur", "eur", 135),
        ("price_basis", "text", 180),
    ],
    "transport_raw": [
        ("date", "date", 90),
        ("trip_id", "text", 170),
        ("type", "text", 90),
        ("carrier", "text", 110),
        ("price_eur_pp", "eur", 110),
        ("duration_min", "integer", 110),
        ("departure", "text", 110),
        ("arrival", "text", 110),
        ("stops", "integer", 70),
        ("booking_link", "link", 240),
    ],
    "daily_top2": [
        ("date", "date", 90),
        ("rank", "integer", 60),
        ("type", "text", 110),
        ("name", "text", 260),
        ("price_eur", "eur", 95),
        ("composite_score", "number", 130),
        ("vs_yesterday_pct", "percent", 130),
        ("vs_7d_avg_pct", "percent", 130),
        ("alert_triggered", "bool", 110),
        ("link", "link", 220),
        ("hotel_id", "text", 110),
        ("rooms", "integer", 70),
        ("total_group_cost_eur", "eur", 135),
        ("price_basis", "text", 180),
    ],
    "accommodation_stats": [
        ("hotel_id", "text", 120),
        ("source", "text", 100),
        ("name", "text", 260),
        ("first_seen", "date", 95),
        ("last_seen", "date", 95),
        ("days_seen", "integer", 90),
        ("recommended_count", "integer", 145),
        ("rank1_count", "integer", 100),
        ("first_recommended", "date", 130),
        ("last_recommended", "date", 130),
        ("latest_price", "eur", 100),
        ("min_price", "eur", 95),
        ("max_price", "eur", 95),
        ("avg_price", "eur", 95),
        ("latest_score", "number", 105),
        ("latest_distance_km", "number", 130),
        ("price_trend", "text", 110),
        ("latest_vs_first_pct", "percent", 135),
        ("latest_vs_7d_avg_pct", "percent", 145),
        ("price_history", "text", 360),
        ("link", "link", 220),
    ],
    "alerts_log": [
        ("timestamp", "datetime", 140),
        ("alert_type", "text", 110),
        ("property_name", "text", 240),
        ("prev_price", "eur", 100),
        ("new_price", "eur", 100),
        ("change_pct", "percent", 110),
        ("notified_via", "text", 110),
    ],
}

# Backwards-compatible header-only view (used by read_accommodation_history etc.)
WORKSHEETS = {name: [c[0] for c in cols] for name, cols in COLUMNS.items()}

# Google Sheets number-format patterns per logical type.
_NUMBER_FORMAT = {
    "date": ("DATE", "yyyy-mm-dd"),
    "datetime": ("DATE_TIME", "yyyy-mm-dd hh:mm"),
    "eur": ("CURRENCY", "[$€]#,##0.00"),
    "number": ("NUMBER", "#,##0.00"),
    "integer": ("NUMBER", "#,##0"),
    "percent": ("NUMBER", "+0.0%;-0.0%;0.0%"),
}

# Header style: bold white text on a deep blue background.
_HEADER_FORMAT = {
    "backgroundColor": {"red": 0.18, "green": 0.31, "blue": 0.55},
    "textFormat": {
        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
        "bold": True,
        "fontSize": 11,
    },
    "horizontalAlignment": "CENTER",
    "verticalAlignment": "MIDDLE",
    "wrapStrategy": "WRAP",
}

# Banded rows (alternating) — applied as conditional format using sheet "alternating colors"
_BAND_LIGHT = {"red": 1.0, "green": 1.0, "blue": 1.0}
_BAND_ALT = {"red": 0.95, "green": 0.97, "blue": 1.0}


def _col_letter(idx: int) -> str:
    """0-indexed column to A1 letter (0->A, 25->Z, 26->AA)."""
    s = ""
    n = idx
    while True:
        s = chr(65 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            return s


def _to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _pct_decimal(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return round((new - old) / old, 4)


def _plain_number(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _price_trend(prices: list[float]) -> str:
    if len(prices) < 2:
        return "new"
    change = _pct_decimal(prices[-1], prices[0])
    if change is None:
        return "new"
    if change <= -0.01:
        return "down"
    if change >= 0.01:
        return "up"
    return "stable"


def _build_accommodation_stats(acc_df: pd.DataFrame, top_df: pd.DataFrame) -> list[list[Any]]:
    """Summarise accommodation history + recommendation frequency."""
    if acc_df is None or acc_df.empty:
        return []

    acc = acc_df.copy()
    for col in ("price_eur", "composite_score", "distance_km"):
        if col in acc:
            acc[col] = _to_number(acc[col])
    if "date" in acc:
        acc["date"] = acc["date"].astype(str)

    top = pd.DataFrame() if top_df is None else top_df.copy()
    if not top.empty:
        if "date" in top:
            top["date"] = top["date"].astype(str)
        if "rank" in top:
            top["rank"] = _to_number(top["rank"])

    rows: list[list[Any]] = []
    for hotel_id, group in acc.groupby("hotel_id", dropna=True):
        group = group.sort_values("date")
        latest = group.iloc[-1]
        price_series = group["price_eur"].dropna() if "price_eur" in group else pd.Series(dtype=float)
        prices = [float(v) for v in price_series.tolist()]
        recent_prices = prices[-7:]
        latest_price = prices[-1] if prices else None
        first_price = prices[0] if prices else None
        avg_7d = round(sum(recent_prices) / len(recent_prices), 2) if recent_prices else None

        if not top.empty:
            matches = pd.DataFrame()
            if "hotel_id" in top:
                matches = top[top["hotel_id"].astype(str) == str(hotel_id)]
            if matches.empty:
                latest_link = str(latest.get("booking_link", ""))
                latest_name = str(latest.get("name", ""))
                link_matches = (
                    top[top["link"].astype(str) == latest_link]
                    if "link" in top and latest_link
                    else pd.DataFrame()
                )
                name_matches = (
                    top[top["name"].astype(str) == latest_name]
                    if "name" in top and latest_name
                    else pd.DataFrame()
                )
                matches = pd.concat([link_matches, name_matches]).drop_duplicates()
        else:
            matches = pd.DataFrame()

        match_dates = sorted(matches["date"].dropna().astype(str).tolist()) if not matches.empty and "date" in matches else []
        price_history = " | ".join(
            f"{row['date']}: EUR {float(row['price_eur']):g}"
            for _, row in group.dropna(subset=["price_eur"]).tail(10).iterrows()
        )
        latest_vs_first = _pct_decimal(latest_price, first_price) if len(prices) >= 2 else None
        latest_vs_7d = _pct_decimal(latest_price, avg_7d) if len(recent_prices) >= 2 else None

        rows.append([
            hotel_id,
            latest.get("source"),
            latest.get("name"),
            group["date"].iloc[0],
            group["date"].iloc[-1],
            int(group["date"].nunique()),
            int(len(matches)),
            int((matches["rank"] == 1).sum()) if not matches.empty and "rank" in matches else 0,
            match_dates[0] if match_dates else None,
            match_dates[-1] if match_dates else None,
            latest_price,
            float(price_series.min()) if not price_series.empty else None,
            float(price_series.max()) if not price_series.empty else None,
            round(float(price_series.mean()), 2) if not price_series.empty else None,
            _plain_number(latest.get("composite_score")),
            _plain_number(latest.get("distance_km")),
            _price_trend(recent_prices),
            latest_vs_first,
            latest_vs_7d,
            price_history,
            latest.get("booking_link"),
        ])

    rows.sort(key=lambda r: (-(r[6] or 0), str(r[2] or "")))
    return rows


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

    @property
    def spreadsheet_url(self) -> str | None:
        """Return the Google Sheet URL when connected."""
        return getattr(self._spreadsheet, "url", None) if self._spreadsheet else None

    def _connect(self) -> None:
        import gspread
        from google.oauth2.service_account import Credentials

        raw = os.environ["GOOGLE_SHEETS_CREDENTIALS"]
        info = json.loads(base64.b64decode(raw))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        client = gspread.authorize(creds)
        self._spreadsheet = client.open(self.spreadsheet_name)
        self._ensure_worksheets()

    # ------------------------------------------------------------------
    # Setup / formatting
    # ------------------------------------------------------------------

    def _ensure_worksheets(self) -> None:
        existing = {ws.title for ws in self._spreadsheet.worksheets()}
        for title, cols in COLUMNS.items():
            if title not in existing:
                ws = self._spreadsheet.add_worksheet(title=title, rows=1000, cols=len(cols))
                ws.append_row([c[0] for c in cols], value_input_option="USER_ENTERED")
                self._format_worksheet(ws, cols)
            else:
                ws = self._spreadsheet.worksheet(title)
                self._ensure_header(ws, cols)

    def _ensure_header(self, ws: Any, cols: list[tuple[str, str, int]]) -> None:
        """Add missing trailing headers and refresh formatting for existing tabs."""
        expected = [c[0] for c in cols]
        current = ws.row_values(1)
        if current[: len(expected)] != expected:
            ws.update(
                range_name="A1",
                values=[expected],
                value_input_option="USER_ENTERED",
            )
            self._format_worksheet(ws, cols)

    def setup(self, reset: bool = False) -> None:
        """Create / reformat all worksheets.

        With reset=True, every worksheet is cleared (data wiped) and
        re-initialised with headers + formatting. Use this once at the start
        of the project to get a clean, nicely-formatted workbook.
        """
        if not self.enabled:
            print("[sheets] not enabled — nothing to set up")
            return
        existing_titles = {ws.title for ws in self._spreadsheet.worksheets()}
        for title, cols in COLUMNS.items():
            if title in existing_titles:
                ws = self._spreadsheet.worksheet(title)
                if reset:
                    ws.clear()
                    ws.update(
                        range_name="A1",
                        values=[[c[0] for c in cols]],
                        value_input_option="USER_ENTERED",
                    )
            else:
                ws = self._spreadsheet.add_worksheet(title=title, rows=1000, cols=len(cols))
                ws.append_row([c[0] for c in cols], value_input_option="USER_ENTERED")
            self._format_worksheet(ws, cols)
            print(f"[sheets] formatted: {title}")

        # Drop the auto-created default "Sheet1" / "List1" if empty.
        for ws in self._spreadsheet.worksheets():
            if ws.title not in COLUMNS and ws.row_count > 0:
                values = ws.get_all_values()
                if not any(any(cell for cell in row) for row in values):
                    self._spreadsheet.del_worksheet(ws)
                    print(f"[sheets] removed empty default tab: {ws.title}")

    def _format_worksheet(self, ws: Any, cols: list[tuple[str, str, int]]) -> None:
        """Apply header style, freeze row 1, column widths, number formats."""
        n_cols = len(cols)
        last_col = _col_letter(n_cols - 1)

        # 1. Header row format + freeze
        ws.format(f"A1:{last_col}1", _HEADER_FORMAT)
        ws.freeze(rows=1)

        # 2. Header row height (taller for readability)
        try:
            self._batch_update([{
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "ROWS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 36},
                    "fields": "pixelSize",
                }
            }])
        except Exception as exc:  # noqa: BLE001
            print(f"[sheets] header height skip: {exc}")

        # 3. Per-column number formats + widths
        requests: list[dict[str, Any]] = []
        for idx, (_name, ctype, width_px) in enumerate(cols):
            # column width
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": idx,
                        "endIndex": idx + 1,
                    },
                    "properties": {"pixelSize": width_px},
                    "fields": "pixelSize",
                }
            })
            # number format (skip text/link/bool)
            nf = _NUMBER_FORMAT.get(ctype)
            if nf:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 1,    # skip header
                            "startColumnIndex": idx,
                            "endColumnIndex": idx + 1,
                        },
                        "cell": {"userEnteredFormat": {"numberFormat": {
                            "type": nf[0], "pattern": nf[1],
                        }}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                })
            if ctype == "link":
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 1,
                            "startColumnIndex": idx,
                            "endColumnIndex": idx + 1,
                        },
                        "cell": {"userEnteredFormat": {"textFormat": {
                            "foregroundColor": {"red": 0.13, "green": 0.39, "blue": 0.76},
                            "underline": True,
                        }}},
                        "fields": "userEnteredFormat.textFormat",
                    }
                })

        # 4. Autofilter over header row (idempotent: setBasicFilter replaces any existing filter)
        requests.append({
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 0,
                        "startColumnIndex": 0,
                        "endColumnIndex": n_cols,
                    }
                }
            }
        })

        # Apply the idempotent batch first.
        self._batch_update(requests)

        # 5. Banded rows: separate best-effort call. Google rejects addBanding
        # when a banded range already exists on this sheet — that's fine,
        # the existing banding is what we wanted anyway.
        banding_req = [{
            "addBanding": {
                "bandedRange": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 0,
                        "endRowIndex": 1000,
                        "startColumnIndex": 0,
                        "endColumnIndex": n_cols,
                    },
                    "rowProperties": {
                        "headerColor": _HEADER_FORMAT["backgroundColor"],
                        "firstBandColor": _BAND_LIGHT,
                        "secondBandColor": _BAND_ALT,
                    },
                }
            }
        }]
        try:
            self._batch_update(banding_req)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "banded" in msg or "already" in msg or "overlap" in msg:
                pass  # banding already present — fine
            else:
                print(f"[sheets] banding skipped: {exc}")

    def _batch_update(self, requests: list[dict[str, Any]]) -> None:
        self._spreadsheet.batch_update({"requests": requests})

    # ------------------------------------------------------------------
    # Writes (append rows; formatting persists because we set it on the column)
    # ------------------------------------------------------------------

    def _append(self, worksheet: str, rows: list[list[Any]]) -> None:
        if not self.enabled or not rows:
            return
        ws = self._spreadsheet.worksheet(worksheet)
        ws.append_rows(rows, value_input_option="USER_ENTERED")

    def write_accommodation(self, run_date: str, options: list[dict[str, Any]]) -> None:
        rows = [
            [
                run_date, o["hotel_id"], o["source"], o["name"], o["price_eur"],
                o["rating"], o["lat"], o["lng"], o["distance_km"],
                o.get("availability", True), o["booking_link"], o.get("composite_score"),
                o.get("rooms"), o.get("total_group_cost_eur"), o.get("price_basis"),
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
            # Store percent as a decimal so the % format works correctly (e.g. -0.112 -> -11.2%)
            vs_y = pick.get("vs_yesterday_pct")
            vs_7 = pick.get("vs_7d_avg_pct")
            rows.append(
                [
                    run_date, pick["rank"], "accommodation", pick["name"],
                    pick["price_eur_per_night"], pick["composite_score"],
                    vs_y / 100 if vs_y is not None else None,
                    vs_7 / 100 if vs_7 is not None else None,
                    pick.get("alert_triggered", False), pick["booking_link"],
                    pick.get("hotel_id"),
                    pick.get("rooms"),
                    pick.get("total_group_cost_eur"),
                    pick.get("price_basis"),
                ]
            )
        self._append("daily_top2", rows)

    def write_alerts(self, alerts: list[dict[str, Any]], notified_via: str = "email") -> None:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        rows = [
            [
                now, "price_drop", a["property"], a["prev_price"], a["new_price"],
                a["change_pct"] / 100 if a.get("change_pct") is not None else None,
                notified_via,
            ]
            for a in alerts
        ]
        self._append("alerts_log", rows)

    def refresh_accommodation_stats(self) -> None:
        """Rebuild the accommodation_stats worksheet from raw history + top picks."""
        if not self.enabled:
            return
        acc_df = self._read_worksheet("accommodation_raw")
        top_df = self._read_worksheet("daily_top2")
        rows = _build_accommodation_stats(acc_df, top_df)
        ws = self._spreadsheet.worksheet("accommodation_stats")
        headers = WORKSHEETS["accommodation_stats"]
        ws.clear()
        ws.update(
            range_name="A1",
            values=[headers] + rows,
            value_input_option="USER_ENTERED",
        )
        self._format_worksheet(ws, COLUMNS["accommodation_stats"])
        print(f"[sheets] refreshed accommodation_stats ({len(rows)} rows)")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _read_worksheet(self, title: str) -> pd.DataFrame:
        ws = self._spreadsheet.worksheet(title)
        records = ws.get_all_records()
        if records:
            return pd.DataFrame(records)
        return pd.DataFrame(columns=WORKSHEETS[title])

    def read_accommodation_history(self) -> pd.DataFrame:
        if not self.enabled:
            return pd.DataFrame(columns=WORKSHEETS["accommodation_raw"])
        df = self._read_worksheet("accommodation_raw")
        if not df.empty:
            df["price_eur"] = pd.to_numeric(df["price_eur"], errors="coerce")
        return df

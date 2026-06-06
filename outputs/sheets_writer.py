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
import math
import numbers
import os
import re
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
        ("price_per_person_per_night_eur", "eur", 135),
        ("rating", "number", 70),
        ("lat", "number", 90),
        ("lng", "number", 90),
        ("distance_km", "number", 100),
        ("availability", "bool", 100),
        ("booking_link", "link", 220),
        ("composite_score", "number", 110),
        ("rooms", "integer", 70),
        ("total_group_cost_eur", "eur", 135),
        ("price_per_person_total_stay_eur", "eur", 155),
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
        ("price_per_person_per_night_eur", "eur", 135),
        ("composite_score", "number", 130),
        ("vs_yesterday_pct", "percent", 130),
        ("vs_7d_avg_pct", "percent", 130),
        ("alert_triggered", "bool", 110),
        ("link", "link", 220),
        ("hotel_id", "text", 110),
        ("rooms", "integer", 70),
        ("total_group_cost_eur", "eur", 135),
        ("price_per_person_total_stay_eur", "eur", 155),
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
        ("latest_price_per_person_per_night", "eur", 150),
        ("min_price_per_person_per_night", "eur", 145),
        ("max_price_per_person_per_night", "eur", 145),
        ("avg_price_per_person_per_night", "eur", 145),
        ("latest_score", "number", 105),
        ("latest_distance_km", "number", 130),
        ("price_trend", "text", 110),
        ("latest_vs_first_pct", "percent", 135),
        ("latest_vs_7d_avg_pct", "percent", 145),
        ("price_history", "text", 360),
        ("link", "link", 220),
    ],
    "accommodation_price_chart": [
        ("date", "date", 95),
        ("hotel_1", "eur", 145),
        ("hotel_2", "eur", 145),
        ("hotel_3", "eur", 145),
        ("hotel_4", "eur", 145),
        ("hotel_5", "eur", 145),
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


def _json_safe(value: Any) -> Any:
    """Convert pandas/numpy missing values and non-finite floats to JSON-safe values."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        return int(numeric) if numeric.is_integer() else numeric
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def _extract_hyperlink_url(value: str) -> str:
    """Return the URL from a HYPERLINK formula, or the original string."""
    match = re.match(r'^=HYPERLINK\("((?:[^"]|"")*)"', value.strip(), flags=re.IGNORECASE)
    if not match:
        return value
    return match.group(1).replace('""', '"')


def _sheet_hyperlink(url: Any, label: Any | None = None) -> str | None:
    """Return plain URL text for Sheets cells; hyperlink metadata is applied separately."""
    if url is None:
        return None
    url_text = _extract_hyperlink_url(str(url).strip())
    if not url_text:
        return None
    return url_text


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


def _with_price_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """Keep old/new sheet price headers readable by existing code."""
    if df is None or df.empty:
        return df
    out = df.copy()
    for alias in ("price_per_person_per_night_eur", "price_eur_per_person_per_night"):
        if "price_eur" not in out and alias in out:
            out["price_eur"] = out[alias]
    if "price_eur_per_person_per_night" not in out and "price_eur" in out:
        out["price_eur_per_person_per_night"] = out["price_eur"]
    if "price_per_person_per_night_eur" not in out and "price_eur" in out:
        out["price_per_person_per_night_eur"] = out["price_eur"]
    return out


def _build_accommodation_stats(acc_df: pd.DataFrame, top_df: pd.DataFrame) -> list[list[Any]]:
    """Summarise accommodation history + recommendation frequency."""
    if acc_df is None or acc_df.empty:
        return []

    acc = _with_price_aliases(acc_df)
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
            _sheet_hyperlink(latest.get("booking_link")),
        ])

    rows.sort(key=lambda r: (-(r[6] or 0), str(r[2] or "")))
    return rows


def _build_accommodation_chart_table(
    acc_df: pd.DataFrame,
    top_df: pd.DataFrame,
    max_series: int = 5,
) -> tuple[list[str], list[list[Any]]]:
    """Build a wide date x hotel price table for Google Sheets line charts."""
    if acc_df is None or acc_df.empty:
        return ["date"], []

    acc = _with_price_aliases(acc_df)
    if "hotel_id" not in acc or "date" not in acc or "price_eur" not in acc:
        return ["date"], []
    acc = acc.copy()
    acc["date"] = acc["date"].astype(str)
    acc["price_eur"] = pd.to_numeric(acc["price_eur"], errors="coerce")
    acc = acc.dropna(subset=["hotel_id", "date", "price_eur"])
    if acc.empty:
        return ["date"], []

    stats = _build_accommodation_stats(acc, top_df)
    ordered_ids = [str(row[0]) for row in stats if row[5] >= 2 and row[10] is not None]
    if not ordered_ids:
        grouped = acc.groupby("hotel_id")["price_eur"].count()
        ordered_ids = [str(hid) for hid, count in grouped.sort_values(ascending=False).items() if count >= 2]
    selected_ids = ordered_ids[:max_series]
    if not selected_ids:
        return ["date"], []

    latest_names = (
        acc.sort_values("date")
        .groupby("hotel_id", dropna=True)["name"]
        .last()
        .to_dict()
    )
    headers = ["date"] + [
        str(latest_names.get(hid) or hid)[:60]
        for hid in selected_ids
    ]

    pivot = (
        acc[acc["hotel_id"].astype(str).isin(selected_ids)]
        .pivot_table(index="date", columns="hotel_id", values="price_eur", aggfunc="last")
        .sort_index()
    )
    rows = []
    for date, values in pivot.iterrows():
        rows.append([date] + [values.get(hid) for hid in selected_ids])
    return headers, rows


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
                values=_json_safe([expected]),
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
                        values=_json_safe([[c[0] for c in cols]]),
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
        self._spreadsheet.batch_update(_json_safe({"requests": requests}))

    # ------------------------------------------------------------------
    # Writes (append rows; formatting persists because we set it on the column)
    # ------------------------------------------------------------------

    def _append(self, worksheet: str, rows: list[list[Any]]) -> int | None:
        if not self.enabled or not rows:
            return None
        ws = self._spreadsheet.worksheet(worksheet)
        start_row = len(ws.get_all_values()) + 1
        ws.append_rows(_json_safe(rows), value_input_option="USER_ENTERED")
        return start_row

    def _apply_hyperlinks(
        self,
        worksheet: str,
        start_row: int | None,
        col_index: int,
        urls: list[Any],
    ) -> None:
        """Apply clickable link metadata to plain URL cells.

        `start_row` is 1-based. `col_index` is 0-based.
        """
        if not self.enabled or start_row is None or not urls:
            return
        ws = self._spreadsheet.worksheet(worksheet)
        rows = []
        for url in urls:
            url_text = _sheet_hyperlink(url)
            if url_text:
                rows.append({
                    "values": [{
                        "userEnteredFormat": {
                            "textFormat": {
                                "link": {"uri": url_text},
                                "foregroundColor": {"red": 0.13, "green": 0.39, "blue": 0.76},
                                "underline": True,
                            }
                        }
                    }]
                })
            else:
                rows.append({"values": [{}]})
        self._batch_update([{
            "updateCells": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": start_row - 1,
                    "endRowIndex": start_row - 1 + len(urls),
                    "startColumnIndex": col_index,
                    "endColumnIndex": col_index + 1,
                },
                "rows": rows,
                "fields": "userEnteredFormat.textFormat",
            }
        }])

    def write_accommodation(self, run_date: str, options: list[dict[str, Any]]) -> None:
        group_size = max(1, int(self.config["trip"]["group_size"]))
        rows = [
            [
                run_date, o["hotel_id"], o["source"], o["name"], o["price_eur"],
                o["rating"], o["lat"], o["lng"], o["distance_km"],
                o.get("availability", True), _sheet_hyperlink(o["booking_link"]), o.get("composite_score"),
                o.get("rooms"), o.get("total_group_cost_eur"),
                round(o["total_group_cost_eur"] / group_size, 2)
                if o.get("total_group_cost_eur") is not None else None,
            ]
            for o in options
        ]
        start_row = self._append("accommodation_raw", rows)
        self._apply_hyperlinks("accommodation_raw", start_row, 10, [o.get("booking_link") for o in options])

    def write_transport(self, run_date: str, options: list[dict[str, Any]]) -> None:
        rows = [
            [
                run_date, o["trip_id"], o["type"], o["carrier"],
                o["price_eur_per_person"], o["duration_min"], o["departure"],
                o["arrival"], o["stops"], _sheet_hyperlink(o["booking_link"]),
            ]
            for o in options
        ]
        start_row = self._append("transport_raw", rows)
        self._apply_hyperlinks("transport_raw", start_row, 9, [o.get("booking_link") for o in options])

    def write_daily_top2(self, run_date: str, analysis: dict[str, Any]) -> None:
        group_size = max(1, int(self.config["trip"]["group_size"]))
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
                    pick.get("alert_triggered", False), _sheet_hyperlink(pick["booking_link"]),
                    pick.get("hotel_id"),
                    pick.get("rooms"),
                    pick.get("total_group_cost_eur"),
                    round(pick["total_group_cost_eur"] / group_size, 2)
                    if pick.get("total_group_cost_eur") is not None else None,
                ]
            )
        start_row = self._append("daily_top2", rows)
        links = [pick.get("booking_link") for pick in analysis["accommodation"]["top_picks"]]
        self._apply_hyperlinks("daily_top2", start_row, 9, links)

    def write_alerts(self, alerts: list[dict[str, Any]], notified_via: str = "email") -> None:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        rows = [
            [
                now, a.get("alert_type", "price_drop"), a["property"], a["prev_price"], a["new_price"],
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
            values=_json_safe([headers] + rows),
            value_input_option="USER_ENTERED",
        )
        self._format_worksheet(ws, COLUMNS["accommodation_stats"])
        self._apply_hyperlinks("accommodation_stats", 2, 20, [row[20] for row in rows])
        self.refresh_accommodation_price_chart(acc_df, top_df)
        print(f"[sheets] refreshed accommodation_stats ({len(rows)} rows)")

    def refresh_accommodation_price_chart(
        self,
        acc_df: pd.DataFrame | None = None,
        top_df: pd.DataFrame | None = None,
    ) -> None:
        """Refresh chart-ready accommodation price data and embedded chart."""
        if not self.enabled:
            return
        acc_df = self._read_worksheet("accommodation_raw") if acc_df is None else acc_df
        top_df = self._read_worksheet("daily_top2") if top_df is None else top_df
        headers, rows = _build_accommodation_chart_table(acc_df, top_df)
        ws = self._spreadsheet.worksheet("accommodation_price_chart")
        ws.clear()
        ws.update(
            range_name="A1",
            values=_json_safe([headers] + rows),
            value_input_option="USER_ENTERED",
        )
        self._format_worksheet(ws, COLUMNS["accommodation_price_chart"])
        self._replace_accommodation_price_chart(ws, len(rows) + 1, len(headers))
        print(f"[sheets] refreshed accommodation_price_chart ({len(rows)} rows)")

    def _replace_accommodation_price_chart(self, ws: Any, row_count: int, col_count: int) -> None:
        """Replace embedded charts on the accommodation chart sheet."""
        metadata = self._spreadsheet.fetch_sheet_metadata()
        sheet_meta = next(
            (s for s in metadata.get("sheets", []) if s.get("properties", {}).get("sheetId") == ws.id),
            {},
        )
        requests: list[dict[str, Any]] = [
            {"deleteEmbeddedObject": {"objectId": chart["chartId"]}}
            for chart in sheet_meta.get("charts", [])
            if "chartId" in chart
        ]
        if row_count >= 3 and col_count >= 2:
            requests.append({
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": "Accommodation price history",
                            "basicChart": {
                                "chartType": "LINE",
                                "legendPosition": "RIGHT_LEGEND",
                                "axis": [
                                    {"position": "BOTTOM_AXIS", "title": "Date"},
                                    {"position": "LEFT_AXIS", "title": "EUR / person / night"},
                                ],
                                "domains": [{
                                    "domain": {"sourceRange": {"sources": [{
                                        "sheetId": ws.id,
                                        "startRowIndex": 0,
                                        "endRowIndex": row_count,
                                        "startColumnIndex": 0,
                                        "endColumnIndex": 1,
                                    }]}}
                                }],
                                "series": [
                                    {"series": {"sourceRange": {"sources": [{
                                        "sheetId": ws.id,
                                        "startRowIndex": 0,
                                        "endRowIndex": row_count,
                                        "startColumnIndex": idx,
                                        "endColumnIndex": idx + 1,
                                    }]}}}
                                    for idx in range(1, col_count)
                                ],
                            },
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {
                                    "sheetId": ws.id,
                                    "rowIndex": 0,
                                    "columnIndex": max(col_count + 1, 7),
                                },
                                "offsetXPixels": 20,
                                "offsetYPixels": 10,
                                "widthPixels": 760,
                                "heightPixels": 380,
                            }
                        },
                    }
                }
            })
        if requests:
            self._batch_update(requests)

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
            df = _with_price_aliases(df)
            df["price_eur"] = pd.to_numeric(df["price_eur"], errors="coerce")
        return df

    def read_transport_history(self) -> pd.DataFrame:
        if not self.enabled:
            return pd.DataFrame(columns=WORKSHEETS["transport_raw"])
        df = self._read_worksheet("transport_raw")
        if not df.empty and "price_eur_pp" in df:
            df["price_eur_pp"] = pd.to_numeric(df["price_eur_pp"], errors="coerce")
        return df

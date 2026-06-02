"""Orchestrator — coordinates all subagents for one daily run.

Pipeline:
  1. load config
  2. fetch accommodation + flights + flixbus
  3. read price history from Sheets, run price-tracker alerts
  4. send scored data to the AI analyser -> structured JSON (validated)
  5. write raw + top2 + alerts to Sheets
  6. send the Gmail digest

Run:
  python agents/orchestrator.py            # live (uses whatever secrets are set)
  python agents/orchestrator.py --dry-run  # mock data, no external writes
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.accommodation_agent import (  # noqa: E402
    fetch_accommodation,
    get_last_errors as _accommodation_errors,
)
from agents.analyser import OUTPUT_SCHEMA_HINT, analyse  # noqa: E402
from agents.critic import critique  # noqa: E402
from agents.flights_agent import fetch_flights, get_last_errors as _flight_errors  # noqa: E402
from agents.flixbus_agent import fetch_flixbus, get_last_errors as _flixbus_errors  # noqa: E402
from agents.price_tracker import check_price_alerts  # noqa: E402
from models import AgentOutput  # noqa: E402
from outputs import gmail_sender  # noqa: E402
from outputs.sheets_writer import SheetsWriter  # noqa: E402
from utils.config_loader import load_config  # noqa: E402

MAX_CRITIC_RETRIES = 2


def run(dry_run: bool = False) -> dict:
    # Load local .env if present (no-op in CI, where secrets come from env).
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    config = load_config()
    run_date = dt.date.today().isoformat()
    print(f"=== Daily travel agent run {run_date} (dry_run={dry_run}) ===")

    # 1–2. Fetch data
    accommodation = fetch_accommodation(config, dry_run=dry_run)
    accommodation_warnings = _accommodation_errors()
    flights = fetch_flights(config, dry_run=dry_run)
    flixbus = fetch_flixbus(config, dry_run=dry_run)
    transport = flights + flixbus
    transport_warnings = _flight_errors() + _flixbus_errors()
    print(f"  fetched {len(accommodation)} stays, {len(transport)} transport options")
    if accommodation_warnings:
        print(f"  accommodation warnings: {accommodation_warnings}")
    if transport_warnings:
        print(f"  transport API warnings: {transport_warnings}")

    # 3. Price history + alerts
    sheets = SheetsWriter(config, dry_run=dry_run)
    history = sheets.read_accommodation_history()
    alerts = check_price_alerts(accommodation, history, config)
    print(f"  {len(alerts)} alert(s) triggered")

    # 4. AI analyser -> structured output -> critic -> retry loop -> Pydantic validation
    raw_analysis = analyse(run_date, accommodation, transport, alerts, config)
    final_verdict: dict[str, Any] = {"valid": True, "issues": [], "retry_hint": None}
    for attempt in range(MAX_CRITIC_RETRIES + 1):
        verdict = critique(accommodation, transport, raw_analysis, OUTPUT_SCHEMA_HINT)
        final_verdict = verdict
        if verdict.get("valid"):
            print(f"  critic OK (attempt {attempt + 1})")
            break
        print(f"  critic FAIL attempt {attempt + 1}: {verdict.get('issues')}")
        if attempt == MAX_CRITIC_RETRIES:
            print("  critic still failing — proceeding with warning banner")
            break
        # Retry analyser with critic feedback baked into the input
        raw_analysis = analyse(
            run_date, accommodation, transport, alerts, config,
            critic_feedback=verdict,
        )

    analysis = AgentOutput.model_validate(raw_analysis)
    analysis_dict = analysis.model_dump()
    analysis_dict["_critic"] = final_verdict   # surfaced in the email if invalid
    analysis_dict["_transport_warnings"] = transport_warnings  # API failures, shown in email
    analysis_dict["_accommodation_warnings"] = accommodation_warnings  # fallback notices, shown in email
    analysis_dict["_spreadsheet_url"] = (
        sheets.spreadsheet_url or config.get("sheets", {}).get("spreadsheet_url")
    )

    # 5. Persist to Sheets (no-op without credentials)
    if not dry_run:
        sheets.write_accommodation(run_date, accommodation)
        sheets.write_transport(run_date, transport)
        sheets.write_daily_top2(run_date, analysis_dict)
        if alerts:
            sheets.write_alerts(alerts)

    # 6. Email digest (preview-only without credentials)
    if not dry_run:
        gmail_sender.send(analysis_dict, history, config)
    else:
        Path(gmail_sender._PREVIEW_PATH).write_text(
            gmail_sender.build_html(analysis_dict, history, config), encoding="utf-8"
        )
        print(f"  digest preview written to {gmail_sender._PREVIEW_PATH}")

    print("=== run complete ===")
    return analysis_dict


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the full daily travel agent")
    parser.add_argument("--dry-run", action="store_true", help="mock data, no external writes")
    parser.add_argument("--print", action="store_true", help="print the analysis JSON")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    if args.print:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

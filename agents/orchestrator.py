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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.accommodation_agent import fetch_accommodation  # noqa: E402
from agents.analyser import analyse  # noqa: E402
from agents.flights_agent import fetch_flights  # noqa: E402
from agents.flixbus_agent import fetch_flixbus  # noqa: E402
from agents.price_tracker import check_price_alerts  # noqa: E402
from models import AgentOutput  # noqa: E402
from outputs import gmail_sender  # noqa: E402
from outputs.sheets_writer import SheetsWriter  # noqa: E402
from utils.config_loader import load_config  # noqa: E402


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
    flights = fetch_flights(config, dry_run=dry_run)
    flixbus = fetch_flixbus(config, dry_run=dry_run)
    transport = flights + flixbus
    print(f"  fetched {len(accommodation)} stays, {len(transport)} transport options")

    # 3. Price history + alerts
    sheets = SheetsWriter(config, dry_run=dry_run)
    history = sheets.read_accommodation_history()
    alerts = check_price_alerts(accommodation, history, config)
    print(f"  {len(alerts)} alert(s) triggered")

    # 4. AI analyser -> structured output, validated against the Pydantic schema
    raw_analysis = analyse(run_date, accommodation, transport, alerts, config)
    analysis = AgentOutput.model_validate(raw_analysis)
    analysis_dict = analysis.model_dump()

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

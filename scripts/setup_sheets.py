"""One-off script to (re)initialise the Google Sheet with nice formatting.

Run locally:
  python scripts/setup_sheets.py            # create missing sheets, format all
  python scripts/setup_sheets.py --reset    # WIPE all data, reformat all

Uses GOOGLE_SHEETS_CREDENTIALS from your local .env (loaded via python-dotenv).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from outputs.sheets_writer import SheetsWriter  # noqa: E402
from utils.config_loader import load_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up + format the Google Sheet")
    parser.add_argument(
        "--reset", action="store_true",
        help="wipe existing data in all 4 worksheets before reformatting",
    )
    args = parser.parse_args()

    config = load_config()
    writer = SheetsWriter(config)
    if not writer.enabled:
        print("ERROR: GOOGLE_SHEETS_CREDENTIALS not set — cannot connect.")
        sys.exit(1)

    print(f"Connected to spreadsheet: {writer.spreadsheet_name}")
    confirm = "Y"
    if args.reset:
        confirm = input("⚠️  --reset will WIPE all data. Type Y to proceed: ").strip()
    if confirm.upper() != "Y":
        print("Aborted.")
        return

    writer.setup(reset=args.reset)
    print("\n✅ Done. Open the sheet to inspect formatting.")


if __name__ == "__main__":
    main()

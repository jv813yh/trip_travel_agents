# AGENT.md

Guidance for coding agents working in this repository.

## Project Snapshot

This is a Python travel-monitoring agent for a Warsaw trip. It fetches accommodation, flight, and FlixBus options, scores stays, compares accommodation prices against Google Sheets history, asks an analyser/critic pair for a structured recommendation, writes results to Sheets, and sends or previews a Gmail HTML digest.

The project is no longer greenfield. Core modules already exist:

- `agents/`: accommodation, flights, FlixBus, price tracking, analyser, critic, orchestrator
- `utils/`: config loading, distance calculation, composite scoring
- `models/`: Pydantic v2 output schema
- `outputs/`: Google Sheets writer and Gmail digest builder/sender
- `scripts/setup_sheets.py`: one-off Sheets setup/formatting
- `.github/workflows/daily_agent.yml`: weekday scheduled GitHub Actions run

Older guidance in `CLAUDE.md` and `AGENTS.md` contains useful design context, but both files currently show encoding mojibake and some stale "greenfield" language. Treat this file as the current operational map.

## Runtime

- Local machine: Windows PowerShell.
- Local venv: `.venv/`.
- CI: Ubuntu via GitHub Actions.
- Workflow Python: `3.12`.
- Requirements are in `requirements.txt`.

Use the local venv when running commands:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python agents/orchestrator.py --dry-run --print
```

Dry-run behavior is important: it uses mock data where appropriate, performs no external writes, and writes an HTML email preview to `outputs/_last_digest.html`.

## Configuration

All trip values must come from `config.yaml`; do not hardcode dates, budgets, addresses, group size, or sheet names in agents.

Current config includes:

- Trip: Kosice to Warsaw, outbound `2026-08-20`, return `2026-08-23`, group size `5`.
- Accommodation target: `sw. Franciszka Salezego 2, 00-392 Warszawa, Poland`.
- Target coordinates are present and used by `utils.distance`.
- Accommodation budget: `70 EUR/night`.
- Sources: `booking_com` primary and `skyscanner` fallback. `airbnb` is commented out.
- Sheet name: `Warszawa Trip Tracker`.

If dates or group size change, update `config.yaml` only.

## Pipeline

`agents/orchestrator.py` is the main entry point:

1. Load `.env` if available.
2. Load `config.yaml`.
3. Fetch accommodation via Apify, then Skyscanner hotels fallback.
4. Fetch flights via Sky-Scrapper/RapidAPI.
5. Fetch FlixBus via RapidAPI.
6. Read accommodation history from Google Sheets.
7. Run `price_tracker.check_price_alerts`, which mutates accommodation rows with trend fields.
8. Run `agents.analyser.analyse`.
9. Run `agents.critic.critique`, retrying analyser up to `MAX_CRITIC_RETRIES`.
10. Validate with `models.AgentOutput`.
11. Write Sheets rows and send Gmail digest when not dry-run.
12. In dry-run, write the preview HTML instead.

## External Services And Environment Variables

Local `.env` is gitignored. GitHub Actions uses repository secrets.

Required for live data/writes:

- `APIFY_TOKEN`: Booking.com/Airbnb actors.
- `RAPIDAPI_KEY`: Sky-Scrapper flights/hotels and FlixBus.
- `GOOGLE_MAPS_API_KEY`: optional precise Distance Matrix distance.
- `GOOGLE_SHEETS_CREDENTIALS`: base64 service-account JSON.
- `GMAIL_CREDENTIALS`: base64 Gmail OAuth credentials JSON.
- `RECIPIENT_EMAIL`: digest recipient.
- `ANTHROPIC_API_KEY`: analyser and critic model calls.

Missing keys should degrade gracefully:

- Accommodation falls back from Apify to Skyscanner, then mock only when no live creds exist.
- Flights and FlixBus return empty lists in production when `RAPIDAPI_KEY` is missing.
- Distance falls back to haversine when target coordinates exist.
- Sheets reads/writes no-op without credentials.
- Gmail writes `outputs/_last_digest.html` without credentials/recipient.
- Analyser/critic use deterministic fallbacks without `ANTHROPIC_API_KEY`.

## Data Contracts

Accommodation rows use keys such as:

- `hotel_id`
- `source`
- `name`
- `price_eur`
- `rating`
- `lat`
- `lng`
- `distance_km`
- `availability`
- `booking_link`
- `composite_score`
- `vs_yesterday_pct`
- `vs_7d_avg_pct`
- `alert_triggered`

Transport rows use:

- `trip_id`
- `type`: `flight` or `flixbus`
- `carrier`
- `price_eur_per_person`
- `duration_min`
- `departure`
- `arrival`
- `stops`
- `booking_link`
- `date`
- `date_offset_days`

Analyser output must validate against `models.schemas.AgentOutput`.

## Scoring

Accommodation composite score is implemented in `utils/scorer.py`:

- Price: 40 points, lower is better, clamped.
- Rating: 35 points, Airbnb 0-5 ratings are normalized to 0-10.
- Distance: 25 points, closer to target is better.
- Result is rounded to one decimal.

Do not duplicate scoring logic elsewhere. Use `composite_score`.

## Known Current Risks

These are worth checking before major work:

- `models.schemas.AccommodationSource` currently allows only `booking_com` and `airbnb`, but live fallback accommodation can have source `skyscanner`. If Skyscanner fallback reaches top picks, Pydantic validation may fail.
- `agents/analyser.py` still has a stale hardcoded system prompt with old dates, budgets, address, and group size. The user message includes config values, but the system prompt should ideally be generated from config to avoid conflicting instructions.
- Many files and docs display mojibake for Slovak/Polish characters and symbols. Preserve behavior first; fix encoding deliberately rather than by broad blind rewrites.
- `.github/workflows/daily_agent.yml` cron comment says CET/UTC+1, but August/Bratislava/Warsaw observe CEST. GitHub cron is UTC, so check the intended local run time if schedule accuracy matters.

## Commands

Install dependencies:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Run full dry-run:

```powershell
python agents/orchestrator.py --dry-run --print
```

Run individual agents:

```powershell
python agents/accommodation_agent.py --dry-run
python agents/flights_agent.py --dry-run
python agents/flixbus_agent.py --dry-run
python models/schemas.py
```

Set up Sheets formatting:

```powershell
python scripts/setup_sheets.py
python scripts/setup_sheets.py --reset
```

Only use `--reset` when the user explicitly wants to wipe sheet data.

## Editing Guidance

- Keep `config.yaml` as the source of truth.
- Preserve graceful degradation paths; daily runs should complete even when optional services fail.
- Prefer small, focused changes and validate with dry-run.
- Do not commit `.env`, generated caches, or local preview artifacts unless explicitly requested.
- When changing output shape, update `models/schemas.py`, `outputs/sheets_writer.py`, `outputs/gmail_sender.py`, analyser fallback, and critic checks together.
- When changing external API normalization, keep stable IDs stable: price history depends on `hotel_id` and `trip_id`.
- When adding an accommodation source, update the Pydantic source literal and email score breakdown behavior if ratings use a different scale.

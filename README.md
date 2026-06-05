# Trip Travel Agent

Automated daily agent that tracks accommodation, flights, and bus options for a group trip to
Warsaw, Poland (Aug 2026). Runs every weekday morning via GitHub Actions, stores price history in
Google Sheets, and emails a daily digest with the top 2 stays + best transport option.

See [CLAUDE.md](CLAUDE.md) for the full design (data sources, Sheets schema, scoring, output schema).

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell:  .venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp config.yaml.example config.yaml   # edit trip parameters
cp .env.example .env                 # add API keys (optional for dry-run)

# Run with mock data, no external calls / writes:
python agents/orchestrator.py --dry-run --print
```

The dry run prints the validated analysis JSON and writes an HTML email preview to
`outputs/_last_digest.html`. No secrets are required — every external call falls back to mock data
when its key is missing.

## Layout

| Path | Role |
|---|---|
| `config.yaml` | All trip parameters — the single source of truth |
| `utils/` | `config_loader`, `scorer` (composite score), `distance` (Maps + haversine fallback) |
| `agents/` | Data-source agents, `price_tracker`, `analyser` (Claude + fallback), `orchestrator` |
| `models/` | Pydantic v2 models validating the analyser's JSON output |
| `outputs/` | `sheets_writer` (Google Sheets), `gmail_sender` (HTML digest + trend chart) |
| `.github/workflows/daily_agent.yml` | Daily cron (07:45 CET, weekdays) |

## Running individual agents

```bash
python agents/accommodation_agent.py --dry-run
python agents/flights_agent.py --dry-run
python agents/flixbus_agent.py --dry-run
python models/schemas.py        # validate schema against the CLAUDE.md example
```

## Secrets

Set these as a local `.env` (gitignored) or as GitHub repository secrets — see the table in
[CLAUDE.md](CLAUDE.md#github-secrets-to-configure). Missing secrets degrade gracefully to mock data
or no-op writes, so the pipeline always completes.

### RapidAPI Kiwi.com Flights subscription

Flights use the Kiwi.com Flights API on RapidAPI. Copy the values from the paid API's code sample:

```text
RAPIDAPI_KEY=<X-RapidAPI-Key from RapidAPI>
RAPIDAPI_KIWI_HOST=kiwi-com-flights-api.p.rapidapi.com
```

The same `RAPIDAPI_KEY` is shared with FlixBus. `RAPIDAPI_KIWI_KEY` is optional and only needed if
you later want a separate key for Kiwi. Optional path overrides are also available:

```text
RAPIDAPI_KIWI_PRICE_MAP_PATH=/api/v1/flights/price-map
RAPIDAPI_KIWI_BOUNDING_BOX=49,14,55,25
```

The configured Kiwi endpoint is `price-map`, so flight prices are indicative map prices. Exact
departure time, arrival time, duration, and booking links are included only when Kiwi returns those
fields; otherwise the email shows the price and a Kiwi search link.

Accommodation does not use Kiwi. Booking.com and Airbnb use Apify.

> Personal, non-commercial use only. Scraping Booking.com / Airbnb may violate their ToS — this
> project uses Apify actors; review the notes in CLAUDE.md.

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

### RapidAPI Sky Scrapper subscription

If RapidAPI shows multiple similarly named Sky/Sky Scrapper APIs, copy the values from the paid
API's code sample:

```text
RAPIDAPI_SKY_KEY=<X-RapidAPI-Key from the paid API/app>
RAPIDAPI_SKY_HOST=<X-RapidAPI-Host from the paid API, for example sky-scrapper3.p.rapidapi.com>
```

`RAPIDAPI_SKY_KEY` falls back to `RAPIDAPI_KEY` when unset. Optional path overrides are also
available if the paid product uses different endpoint paths:

```text
RAPIDAPI_SKY_FLIGHTS_PATH=/scrape
RAPIDAPI_SKY_HOTELS_PATH=/api/v1/hotels/searchHotels
```

The code defaults to the newer `sky-scrapper3.p.rapidapi.com` Ultra host. That product's sample
uses `GET /scrape?target=...`, so flight results are only parsed when the response contains
structured itinerary JSON. If you subscribe to an older structured flights product instead, set
`RAPIDAPI_SKY_HOST=sky-scrapper.p.rapidapi.com` and
`RAPIDAPI_SKY_FLIGHTS_PATH=/api/v1/flights/searchFlights`. The Actions log prints the Sky Scrapper
host on API errors, which helps confirm whether the run is hitting the paid subscription or an
exhausted BASIC subscription.

> Personal, non-commercial use only. Scraping Booking.com / Airbnb may violate their ToS — this
> project uses Apify actors; review the notes in CLAUDE.md.

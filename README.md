# Trip Travel Agent

Daily travel monitor for the Warsaw group trip. The agent fetches accommodation, transport
signals, compares hotel prices against Google Sheets history, and sends a Gmail digest with the
best current picks.

The trip itself is configured in `config.yaml`: origin/destination, dates, group size,
accommodation target address, budgets, alert thresholds, and enabled accommodation sources.

## Current Behaviour

Every scheduled run:

1. Loads trip settings from `config.yaml`.
2. Fetches accommodation from Apify Booking.com and Airbnb actors.
3. Fetches Kiwi.com flight price-map data from RapidAPI.
4. Fetches FlixBus scheduled trips from RapidAPI.
5. Scores accommodation by price, rating, and distance to the target address.
6. Compares hotel prices with historical `accommodation_raw` rows in Google Sheets.
7. Runs the analyser plus critic validation.
8. Writes raw data, top picks, alerts, and accommodation stats to Google Sheets.
9. Sends a Gmail digest with accommodation picks, transport recommendation, alerts, and charts.

## Agents And Workflow

The run starts in `agents/orchestrator.py`. It coordinates the source agents, validation, Sheets
writes, and Gmail delivery.

Agent responsibilities:

- `agents/accommodation_agent.py`: fetches Booking.com and Airbnb options through Apify, normalizes
  prices to EUR per person per night, resolves direct listing links, calculates distance to the
  configured target address, and attaches a composite score.
- `agents/flights_agent.py`: fetches Kiwi.com `price-map` data through RapidAPI, resolves Kiwi
  place slugs through autocomplete, and emits indicative flight price rows. These rows are not
  recommended unless they contain confirmed duration, departure, and arrival.
- `agents/flixbus_agent.py`: fetches FlixBus scheduled trips through RapidAPI and builds booking
  links with configured date and passenger count.
- `agents/price_tracker.py`: compares today's accommodation prices with Google Sheets history and
  labels alerts as `price_drop`, `under_budget`, or `new_under_budget`.
- `agents/analyser.py`: chooses top accommodation and transport output. It uses Claude when
  available and falls back to deterministic logic if the model call fails or returns invalid JSON.
- `agents/critic.py`: validates that selected hotels and transport options exist in the raw data,
  rejects fabricated options, rejects bad Kiwi links, and prevents itinerary-free Kiwi price-map
  rows from becoming flight recommendations.
- `outputs/sheets_writer.py`: reads/writes Google Sheets, maintains raw history tabs, and rebuilds
  `accommodation_stats`.
- `outputs/gmail_sender.py`: builds and sends the Gmail digest with cards, alerts, and inline charts.

Workflow summary:

```text
config.yaml
  -> accommodation_agent + flights_agent + flixbus_agent
  -> price_tracker
  -> analyser
  -> critic
  -> Pydantic schema validation
  -> Google Sheets writes
  -> Gmail digest
```

## Important Data Source Notes

Accommodation uses Apify:

- Booking.com actor: `voyager/booking-scraper`
- Airbnb actor: `trakk/airbnb-scraper`

The accommodation agent prefers direct provider listing links:

- Booking.com links must point to `/hotel/...` when available.
- Airbnb links must point to `/rooms/...` when available.
- If Airbnb gives an ID but no URL, the agent builds `https://www.airbnb.com/rooms/{id}`.
- If a provider only returns a generic search URL, the daily email includes a source warning.

Flights use Kiwi.com Flights API via RapidAPI:

- Endpoint: `/api/v1/flights/price-map`
- Source place is resolved through `/api/v1/places/autocomplete`.
- `price-map` returns city-level indicative prices, not confirmed itineraries.
- Kiwi rows without duration, departure, and arrival are shown as **Flight price signal** only.
- Such Kiwi rows are not allowed to become the recommended “Best flight.”

FlixBus uses RapidAPI and returns scheduled trip data, so FlixBus can be recommended when it has
price, departure, arrival, and duration.

## Config

Main settings live in `config.yaml`.

```yaml
trip:
  origin_city: "Kosice"
  origin_airport: "KSC"
  destination_city: "Warsaw"
  destination_airport: "WAW"
  dates:
    outbound: "2026-08-20"
    return: "2026-08-23"
  group_size: 5

accommodation:
  target_address: "..."
  target_coordinates:
    lat: 52.2401
    lng: 21.0255
  max_price_per_night_eur: 70
  min_rating: 8.5
  max_distance_km: 3.0
  nights: 3
  sources:
    - booking_com
    - airbnb

transport:
  budget_flight_eur: 60
  budget_bus_eur: 25
  max_layovers: 1

alerts:
  price_drop_threshold_pct: 10
  budget_breach_alert: true
```

Some provider identifiers are still code constants because external APIs require their own IDs or
slugs. Examples: Kiwi place slug fallback for `KSC` and FlixBus city UUIDs.

## Alerts

Hotel alerts have explicit types:

- `price_drop`: known hotel dropped by at least `price_drop_threshold_pct`.
- `under_budget`: known hotel is currently below the configured accommodation budget.
- `new_under_budget`: first time seeing this hotel and it is already below budget.

The email separates real historical **Price alerts** from **New Budget Matches**, so first-seen
hotels no longer appear as `EUR None -> EUR ...`.

## Charts

The Gmail digest embeds inline PNG charts generated with matplotlib:

- Accommodation price history by stable `hotel_id`.
- Transport price history by stable `trip_id`.

Accommodation history is based on the `accommodation_raw` sheet. Transport history is based on the
`transport_raw` sheet.

## Google Sheets

The workbook contains:

- `accommodation_raw`
- `transport_raw`
- `daily_top2`
- `accommodation_stats`
- `accommodation_price_chart`
- `alerts_log`

`accommodation_stats` is rebuilt from raw accommodation history and top-pick history after each
successful live run. `accommodation_price_chart` is rebuilt from `accommodation_raw` and contains a
wide chart-ready table plus an embedded Google Sheets line chart when at least one hotel has two or
more valid price points.

## Secrets

Set these locally in `.env` or as GitHub repository secrets:

```text
APIFY_TOKEN=
RAPIDAPI_KEY=
GOOGLE_MAPS_API_KEY=
GOOGLE_SHEETS_CREDENTIALS=
GMAIL_CREDENTIALS=
RECIPIENT_EMAIL=
ANTHROPIC_API_KEY=
```

Optional overrides:

```text
RAPIDAPI_KIWI_HOST=kiwi-com-flights-api.p.rapidapi.com
RAPIDAPI_KIWI_PRICE_MAP_PATH=/api/v1/flights/price-map
RAPIDAPI_KIWI_BOUNDING_BOX=49,14,55,25
RAPIDAPI_KIWI_AUTOCOMPLETE_PATH=/api/v1/places/autocomplete
```

`RAPIDAPI_KEY` is shared by Kiwi and FlixBus. The GitHub Actions workflow pins the Kiwi host and
price-map path.

## Running

Install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Run the full pipeline with mock data:

```powershell
python agents/orchestrator.py --dry-run --print
```

Run the full live pipeline:

```powershell
python agents/orchestrator.py
```

Run individual agents:

```powershell
python agents/accommodation_agent.py --dry-run
python agents/flights_agent.py
python agents/flixbus_agent.py --dry-run
python models/schemas.py
```

## GitHub Actions

`.github/workflows/daily_agent.yml` runs every weekday at `06:45 UTC`, which is `07:45 CET`.
It also supports manual `workflow_dispatch`.

The workflow uses Python 3.12 and runs:

```bash
python agents/orchestrator.py
```

## Validation

The analyser can use Claude when `ANTHROPIC_API_KEY` is set. If Claude returns invalid JSON or the
API call fails, the deterministic fallback is used.

The critic then checks that recommended hotels and transport options exist in raw data. It also
rejects Kiwi flight recommendations when no flight option has confirmed duration, departure, and
arrival.

## Limitations

- Kiwi `price-map` is not an itinerary endpoint. It is useful as a cheap-flight signal, but not as
  proof of a concrete flight schedule.
- Booking.com and Airbnb actor output fields may change. The agent now searches multiple URL fields
  and warns when only generic search links are available.
- Provider prices and availability can change between the daily run and manual booking.

Personal, non-commercial use only. Review Booking.com, Airbnb, Kiwi, and FlixBus terms before use.

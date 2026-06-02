# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

---

# Poland Trip Travel Agent

> Automated daily agent that tracks accommodation, flights, and bus options for a trip to Warsaw, Poland. Runs every morning via GitHub Actions, stores price history in Google Sheets, and sends a daily digest via Gmail.

---

## Current repository state (read first)

**This is a greenfield project — the design below is a build plan, not a description of existing code.** As of this writing the repo contains only this `AGENTS.md` and an empty virtualenv. None of the files in the "Repository structure" section (`agents/`, `outputs/`, `utils/`, `config.yaml`, `requirements.txt`, the GitHub Actions workflow) exist yet. Everything in the sections below is the *intended* design to implement — treat code snippets (composite score, price-tracker logic, output schema) as the target specification.

Environment notes that differ from the spec and must be reconciled when implementing:
- Local venv is **Python 3.11.0** (`.venv/`), with **no packages installed yet**. The spec's GitHub Actions workflow pins Python 3.12 — pick one and align both. Run `pip install -r requirements.txt` (once `requirements.txt` is created) inside the venv before anything else.
- All the dev/test commands in the "Development & testing" section reference files that do not exist yet; they will only work after the corresponding modules are written.
- This is a Windows development machine (PowerShell). The CI workflow targets `ubuntu-latest`.

When starting implementation, the natural order is: `utils/config_loader.py` + `config.yaml` → `utils/scorer.py` → the data-source agents → `outputs/` writers → `agents/orchestrator.py` → the GitHub Actions workflow.

---

## Project overview

This project is a multi-agent system built with Python and orchestrated via GitHub Actions. Every working day at 07:45 (CET) it:

1. Scrapes/fetches accommodation options near a target address in Warsaw
2. Fetches flight prices (Ryanair, Wizz Air via Skyscanner API) for the given travel dates
3. Fetches FlixBus prices for the same dates
4. Calculates a composite score for each option (price + rating + distance)
5. Compares today's prices against historical data and detects significant drops
6. Sends a daily Gmail digest with top 2 picks + links
7. Appends all data to Google Sheets for trend analysis

---

## Trip configuration (`config.yaml`)

All trip parameters live in `config.yaml`. The system prompt and all agents read from this file — never hardcode values in agent code.

```yaml
trip:
  origin_city: "Košice"
  origin_airport: "KSC"
  destination_city: "Warsaw"
  destination_airport: "WAW"          # also check WMI (Modlin)
  dates:
    outbound: "2026-08-07"            # Friday
    return: "2026-08-14"              # Friday
  group_size: 4

accommodation:
  target_address: "ul. Marszałkowska, Warsaw, Poland"   # where friends live
  max_price_per_night_eur: 80
  min_rating: 8.5                     # Booking.com scale 0–10
  max_distance_km: 3.0
  nights: 7
  sources:
    - booking_com
    - airbnb

transport:
  budget_flight_eur: 60               # per person one-way
  budget_bus_eur: 25                  # per person one-way
  max_layovers: 1

alerts:
  price_drop_threshold_pct: 10        # email alert if price drops >10% vs yesterday
  budget_breach_alert: true           # email alert if price goes below max_price_per_night_eur
```

---

## System prompt (passed to the AI analyser agent)

```
You are a travel assistant helping plan a group trip to Warsaw, Poland in August 2026.

GROUP CONTEXT:
- 4 people travelling from Košice, Slovakia
- Friends live near ul. Marszałkowska, Warsaw — accommodation must be as close as possible to this address
- Travel dates: 7–14 August 2026 (7 nights)
- Budget: max €80/night total for accommodation, max €60/person for flights (one-way), max €25/person for FlixBus (one-way)

YOUR TASK:
Given today's scraped data, select the TOP 2 accommodation options and TOP 1 transport option using the following scoring logic:

Accommodation composite score (0–100):
  - Price score: 40 pts  → lower price = higher score (linear from budget max to 50% of budget)
  - Rating score: 35 pts → Booking/Airbnb rating mapped to 0–35
  - Distance score: 25 pts → closer to ul. Marszałkowska = higher score (max 3 km range)

Transport recommendation:
  - Compare cheapest available flight vs FlixBus for the same dates
  - Consider total travel time (door-to-door), not just ticket price
  - Recommend the better option with clear reasoning

Output format: JSON (see output schema below). Be concise. Do not fabricate data — if a field is unavailable, set it to null.
```

---

## Repository structure

```
poland-trip-agent/
├── .github/
│   └── workflows/
│       └── daily_agent.yml          # GitHub Actions cron job
├── agents/
│   ├── orchestrator.py              # coordinates all subagents
│   ├── accommodation_agent.py       # scraping + API calls for stays
│   ├── flights_agent.py             # Skyscanner / Ryanair / Wizz Air
│   ├── flixbus_agent.py             # FlixBus pricing
│   ├── price_tracker.py            # historical comparison + alert logic
│   └── analyser.py                 # AI analyser (Codex) + deterministic fallback
├── models/
│   ├── __init__.py
│   └── schemas.py                  # Pydantic v2 models for the agent output JSON
├── outputs/
│   ├── gmail_sender.py              # sends daily digest email
│   └── sheets_writer.py            # appends data to Google Sheets
├── utils/
│   ├── distance.py                  # Google Maps Distance Matrix API
│   ├── scorer.py                    # composite score calculator
│   └── config_loader.py            # loads config.yaml
├── config.yaml                      # all trip parameters (edit here)
├── requirements.txt
├── AGENTS.md                        # this file
└── README.md
```

---

## Data sources & APIs

### Accommodation

**Recommended approach: Apify actors** (not raw scraping)

Raw Playwright/Puppeteer scraping of Booking.com and Airbnb is fragile — both platforms actively block bots. Use Apify's maintained actors instead:

| Source | Apify actor | Notes |
|---|---|---|
| Booking.com | `voyager/booking-scraper` | Returns hotel_id, price, rating, coordinates, review count |
| Airbnb | `tri_angle/airbnb-scraper` | Returns listing_id, price, rating, coordinates |

Both actors return stable `hotel_id` / `listing_id` fields — store these as primary keys in Google Sheets so price history is correctly attributed to the same property across days.

**Fallback:** If Apify costs are a concern, use RapidAPI's unofficial Booking.com API (`booking-com.p.rapidapi.com`) — free tier allows ~500 requests/month which is sufficient for this use case.

### Flights

Use **Skyscanner Flights Search API** via RapidAPI (`skyscanner50.p.rapidapi.com`):
- Endpoint: `flights/search-roundtrip` or `flights/search-one-way`
- Also directly check Ryanair and Wizz Air public APIs for KSC→WAW/WMI routes
- Store: `flight_id` (route hash: `origin-dest-date-carrier`), carrier, price, duration, stops, booking_link

### FlixBus

Use **FlixBus API** via RapidAPI (`flixbus.p.rapidapi.com`):
- Endpoint: `/search_trips` with origin/destination city IDs (Košice = `39`, Warsaw = `36`)
- Store: `trip_id`, departure_time, arrival_time, price, duration, booking_link

### Distance scoring

Use **Google Maps Distance Matrix API**:
- For each accommodation option: calculate driving/walking distance from property coordinates to `ul. Marszałkowska, Warsaw`
- Cache results in Google Sheets — distance doesn't change, no need to re-fetch daily

---

## Google Sheets structure

### Sheet 1: `accommodation_raw`

| date | hotel_id | source | name | price_eur | rating | lat | lng | distance_km | availability | booking_link | composite_score |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-06-01 | bk_123456 | booking_com | Apartmán Centrum | 72 | 9.1 | 52.229 | 21.012 | 0.8 | true | https://... | 81.4 |

- One row per property per day
- `hotel_id` is the stable Booking/Airbnb internal ID — never changes
- `composite_score` is calculated by `scorer.py` before writing

### Sheet 2: `transport_raw`

| date | trip_id | type | carrier | price_eur_pp | duration_min | departure | arrival | stops | booking_link |
|---|---|---|---|---|---|---|---|---|---|
| 2026-06-01 | KSC-WAW-20260807-FR | flight | Ryanair | 49 | 95 | 06:30 | 08:05 | 0 | https://... |
| 2026-06-01 | KOS-WAW-20260807-FX | flixbus | FlixBus | 22 | 480 | 07:00 | 15:00 | 1 | https://... |

### Sheet 3: `daily_top2`

| date | rank | type | name | price_eur | composite_score | vs_yesterday_pct | vs_7d_avg_pct | alert_triggered | link |
|---|---|---|---|---|---|---|---|---|---|
| 2026-06-01 | 1 | accommodation | Apartmán Centrum | 72 | 81.4 | -11.2 | -8.7 | false | https://... |

- This is the "executive summary" sheet — two rows per day (rank 1 and rank 2)
- Used as the source for the chart in the daily email

### Sheet 4: `alerts_log`

| timestamp | alert_type | property_name | prev_price | new_price | change_pct | notified_via |
|---|---|---|---|---|---|---|
| 2026-06-01 08:03 | price_drop | Apartmán Centrum | 85 | 72 | -15.3 | email |

---

## Price tracking & alert logic (`price_tracker.py`)

```python
def check_price_alerts(today_data: list[dict], history_df: pd.DataFrame, config: dict) -> list[dict]:
    alerts = []
    threshold = config["alerts"]["price_drop_threshold_pct"]
    budget = config["accommodation"]["max_price_per_night_eur"]

    for prop in today_data:
        hid = prop["hotel_id"]
        price_today = prop["price_eur"]

        # filter history for this property
        hist = history_df[history_df["hotel_id"] == hid].sort_values("date")

        if len(hist) == 0:
            continue  # first time seeing this property, no baseline

        # vs yesterday
        yesterday_price = hist.iloc[-1]["price_eur"]
        change_vs_yesterday = (price_today - yesterday_price) / yesterday_price * 100

        # vs 7-day average
        last7 = hist.tail(7)["price_eur"]
        avg_7d = last7.mean()
        change_vs_7d = (price_today - avg_7d) / avg_7d * 100

        prop["vs_yesterday_pct"] = round(change_vs_yesterday, 1)
        prop["vs_7d_avg_pct"] = round(change_vs_7d, 1)
        prop["alert_triggered"] = False

        # trigger alert: price drop > threshold OR price below budget
        if change_vs_yesterday <= -threshold or price_today < budget:
            prop["alert_triggered"] = True
            alerts.append({
                "property": prop["name"],
                "hotel_id": hid,
                "prev_price": yesterday_price,
                "new_price": price_today,
                "change_pct": change_vs_yesterday,
                "link": prop["booking_link"]
            })

    return alerts
```

Alerts are included in the daily Gmail email. There is no Telegram integration — all notifications go via email only.

---

## Gmail daily digest format

Subject: `🏕️ Warsaw Trip Update — {date} | Top pick: €{price}/night`

The email contains:

1. **Transport recommendation of the day** — best flight vs FlixBus comparison with prices, times, and direct booking links
2. **Top 2 accommodation picks** — name, price, rating, distance from ul. Marszałkowska, composite score, direct booking link
3. **Price alerts** (if any) — properties where price dropped significantly vs yesterday
4. **7-day price trend** — a small inline chart image (generated with `matplotlib`, embedded as base64)

Links in the email open directly to the Booking.com / Airbnb / FlixBus / airline booking page.

---

## GitHub Actions workflow (`.github/workflows/daily_agent.yml`)

```yaml
name: Daily travel agent

on:
  schedule:
    - cron: '45 6 * * 1-5'    # 07:45 CET (UTC+1) on weekdays
  workflow_dispatch:            # allow manual trigger for testing

jobs:
  run-agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run orchestrator
        env:
          APIFY_TOKEN: ${{ secrets.APIFY_TOKEN }}
          RAPIDAPI_KEY: ${{ secrets.RAPIDAPI_KEY }}
          GOOGLE_MAPS_API_KEY: ${{ secrets.GOOGLE_MAPS_API_KEY }}
          GOOGLE_SHEETS_CREDENTIALS: ${{ secrets.GOOGLE_SHEETS_CREDENTIALS }}
          GMAIL_CREDENTIALS: ${{ secrets.GMAIL_CREDENTIALS }}
          RECIPIENT_EMAIL: ${{ secrets.RECIPIENT_EMAIL }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: python agents/orchestrator.py
```

### GitHub Secrets to configure

| Secret | Description |
|---|---|
| `APIFY_TOKEN` | Apify API token (for Booking.com + Airbnb actors) |
| `RAPIDAPI_KEY` | RapidAPI key (Skyscanner + FlixBus) |
| `GOOGLE_MAPS_API_KEY` | Google Maps Distance Matrix API |
| `GOOGLE_SHEETS_CREDENTIALS` | Service account JSON (base64 encoded) |
| `GMAIL_CREDENTIALS` | Gmail OAuth2 credentials JSON (base64 encoded) |
| `RECIPIENT_EMAIL` | Your email address |
| `ANTHROPIC_API_KEY` | Codex API key for the AI analyser agent |

---

## Agent output schema (JSON)

The AI analyser agent returns structured JSON. All downstream outputs (Gmail, Sheets) are built from this.

```json
{
  "run_date": "2026-06-01",
  "accommodation": {
    "top_picks": [
      {
        "rank": 1,
        "hotel_id": "bk_123456",
        "source": "booking_com",
        "name": "Apartmán Centrum Warsaw",
        "price_eur_per_night": 72,
        "rating": 9.1,
        "distance_km": 0.8,
        "composite_score": 81.4,
        "vs_yesterday_pct": -11.2,
        "vs_7d_avg_pct": -8.7,
        "alert_triggered": false,
        "booking_link": "https://www.booking.com/hotel/pl/apartman-centrum.html",
        "coordinates": { "lat": 52.229, "lng": 21.012 }
      },
      {
        "rank": 2,
        "hotel_id": "ab_789012",
        "source": "airbnb",
        "name": "Cozy Studio Śródmieście",
        "price_eur_per_night": 65,
        "rating": 4.87,
        "distance_km": 1.4,
        "composite_score": 76.2,
        "vs_yesterday_pct": -3.1,
        "vs_7d_avg_pct": -1.8,
        "alert_triggered": false,
        "booking_link": "https://www.airbnb.com/rooms/789012",
        "coordinates": { "lat": 52.231, "lng": 21.009 }
      }
    ],
    "alerts": []
  },
  "transport": {
    "recommendation": "flight",
    "reasoning": "Ryanair KSC→WAW is €49/person (1h35m). FlixBus is €22/person but takes 8h. For 4 people the flight saves 26 hours of total travel time for €108 extra — worth it.",
    "options": [
      {
        "trip_id": "KSC-WAW-20260807-FR",
        "type": "flight",
        "carrier": "Ryanair",
        "price_eur_per_person": 49,
        "duration_min": 95,
        "departure": "06:30",
        "arrival": "08:05",
        "stops": 0,
        "booking_link": "https://www.ryanair.com/...",
        "total_group_cost_eur": 196
      },
      {
        "trip_id": "KOS-WAW-20260807-FX",
        "type": "flixbus",
        "carrier": "FlixBus",
        "price_eur_per_person": 22,
        "duration_min": 480,
        "departure": "07:00",
        "arrival": "15:00",
        "stops": 1,
        "booking_link": "https://global.flixbus.com/...",
        "total_group_cost_eur": 88
      }
    ]
  }
}
```

---

## Composite score formula

```python
def composite_score(price_eur, rating, distance_km, config) -> float:
    budget = config["accommodation"]["max_price_per_night_eur"]
    min_price = budget * 0.5   # assume ~50% of budget = best possible price

    # Price score (40 pts): linear scale, lower = better
    price_score = max(0, 40 * (1 - (price_eur - min_price) / (budget - min_price)))

    # Rating score (35 pts): Booking scale 0–10, Airbnb scale 0–5 normalised to 0–10
    rating_score = (rating / 10) * 35

    # Distance score (25 pts): 0 km = 25 pts, 3+ km = 0 pts
    max_dist = config["accommodation"]["max_distance_km"]
    distance_score = max(0, 25 * (1 - distance_km / max_dist))

    return round(price_score + rating_score + distance_score, 1)
```

---

## `requirements.txt`

```
anthropic>=0.25.0
apify-client>=1.7.0
requests>=2.31.0
pandas>=2.2.0
gspread>=6.0.0
google-auth>=2.29.0
google-api-python-client>=2.124.0
matplotlib>=3.8.0
python-dotenv>=1.0.0
pyyaml>=6.0.1
```

---

## Development & testing

```bash
# Clone and install
git clone https://github.com/yourname/poland-trip-agent
cd poland-trip-agent
pip install -r requirements.txt

# Copy and edit config
cp config.yaml.example config.yaml

# Set up local .env for secrets (never commit this)
cp .env.example .env

# Run a single agent for testing
python agents/accommodation_agent.py --dry-run

# Run the full orchestrator locally
python agents/orchestrator.py

# Trigger GitHub Actions manually (after push)
# Go to Actions tab → "Daily travel agent" → "Run workflow"
```

---

## Notes & known limitations

- **Booking.com / Airbnb ToS**: scraping these platforms may violate their terms of service. Apify actors operate in a grey area. This project is for personal, non-commercial use only.
- **Price availability**: prices shown are for the specified check-in/check-out dates only. Availability can disappear between scrape and booking.
- **FlixBus Košice**: verify that FlixBus operates a Košice→Warsaw route in August 2026 — this route exists seasonally.
- **Google Maps API cost**: Distance Matrix API charges ~$0.005 per element. With ~20 properties per day and distance caching, monthly cost is negligible (<$0.10).
- **Apify cost**: free tier includes $5 of compute monthly, which covers ~30 daily runs of the Booking scraper at typical depth.
- **GitHub Actions free tier**: 2,000 minutes/month on public repos, 500 on private. Each run takes ~3–5 minutes → well within limits.

# Citi Bike Availability Forecasting

End-to-end machine learning project that forecasts Citi Bike availability at
individual docking stations across New York City, at horizons from 10 minutes
to multiple days ahead. The model is designed to power a consumer-facing web
app that shows commuters predicted bike availability at nearby stations before
they leave home.

## Goals

- Forecast bike availability per station at multiple horizons: 10 min, 1 hr,
  3 hr, 6 hr, 12 hr, 24 hr, and multi-day.
- Station-level model that captures individual dock behavior, not system averages.
- Integrate **forecast** weather as forward-looking features while keeping it
  strictly separate from **observed** weather to avoid leakage at inference time.
- Engineer transit-connectivity features from MTA subway entrance locations
  within 800 m of each station.
- Serve predictions through a live web app and measure user acquisition through
  an Instagram/Meta ad funnel with A/B testing.

## Tech Stack

Python · PostgreSQL · TimescaleDB · XGBoost · scikit-learn · Docker ·
Open-Meteo API · GBFS API · NYC Open Data · Meta Ads

## Architecture

```
GBFS / Open-Meteo / NYC Open Data  →  ingestion scripts  →  TimescaleDB
                                                              │
                                       build_training_features.py
                                                              │
                                          training_features table
                                                              │
                                              train_model.py (14 models)
                                                              │
                                            web app  →  ad funnel / A-B tests
```

## Data Model

All tables are defined in [`sql/schema.sql`](sql/schema.sql). Highlights:

| Table | Contents |
|---|---|
| `station_status` | Live availability snapshots, every ~2.5 min (TimescaleDB hypertable) |
| `station_status_pre2021` | Historical availability 2016–2019 + 2021 (334.5M rows) |
| `station_hourly_flow` | Hourly departures/arrivals per station, 2019–2026 |
| `station_demand_profile` | Avg flow per station per hour-of-day × day-of-week |
| `station_trip_features` | Per-station summary features (member ratio, role, etc.) |
| `station_information` | Station metadata (lat/lon, capacity) |
| `citibike_station_subway_proximity` | Subway-entrance proximity features |
| `weather_*_observed` / `weather_*_forecast` | Observed and forecast weather, pre/post 2021 |

**Data gap:** raw availability snapshots exist only for 2016–2021 and
May 2026–present. Lag-feature-dependent models train on those periods and skip
2022–April 2026.

## Repository Layout

```
citibike/
├── data_ingestion/      Live ingestion scripts + Task Scheduler setup
├── data_historical/     One-time historical backfill scripts
├── citibike/            Shared Python package (config, dataset, features, plots, modeling)
├── model_training/      build_training_features.py, train_model.py (Phases 2–3)
├── notebooks/           EDA, hypothesis tests, modeling (PHASE.NUMBER-description.ipynb)
├── sql/schema.sql       Full database schema
├── reports/figures/     Saved charts
├── web_app/             Web app (Phase 4)
├── data/                raw/interim (gitignored) · processed/external
└── requirements.txt
```

Run Python from the project root so `from citibike... import ...` resolves.

## Setup

```bash
git clone <repo-url>
cd citibike

python -m venv venv
venv\Scripts\activate           # Windows
pip install -r requirements.txt

# configure DB credentials
copy data_ingestion\.env.example data_ingestion\.env   # then edit values
```

Database runs in Docker (TimescaleDB):

```bash
docker run -d --name citibike-db -p 5555:5432 \
  -e POSTGRES_DB=citibike -e POSTGRES_PASSWORD=yourpassword \
  timescale/timescaledb:latest-pg16

psql -h localhost -p 5555 -U postgres -d citibike -f sql/schema.sql
```

## Project Status

- [x] Data ingestion pipeline (GBFS, weather, trips, MTA) — complete and running
- [ ] Phase 2 — build `training_features` table
- [ ] Phase 3 — train & evaluate 14 models (7 horizons × regression + classification)
- [ ] Phase 4 — web app + live deployment
- [ ] Phase 5 — Instagram/Meta ad campaign + A/B testing

## Live App

_Link will be added once deployed._

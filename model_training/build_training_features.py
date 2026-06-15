"""Phase 2 — build the training_features table.

Architecture (post-2026-06-14 pivot): this builder reads from the cleaned,
hourly-sampled table `station_status_hourly_clean` — NOT the raw 334M-row
archive. The cleaning + hourly point-sampling already happened upstream in
model_training/build_clean_availability.py, so this script only assembles
features (lags, weather, static, demand, targets) and never re-cleans anything.

It is still a SQL orchestrator (INSERT ... SELECT run in-DB). The eventual full
pivot re-expresses the feature math in pandas with a month+buffer load; until
then this SQL path works against the clean table and is the simplest thing that
produces correct rows for horizons >= 1 hour.

Per month the builder:
  1. Picks the weather tables by era (pre-2021 vs post-2021) and SKIPS the
     2022-April 2026 gap + 2020 (no clean rows exist there anyway).
  2. Reads the already-hourly base rows straight from station_status_hourly_clean
     (no sampling needed — one row per station-hour by construction).
  3. Computes lags, observed weather, static, and demand features once.
  4. For each horizon, writes target + horizon-matched forecast weather.

HOURLY-DATA CONSEQUENCES (the clean table has no sub-hourly resolution):
  - rate_of_change_10min/20min/30min are NULL here — they need sub-hourly data.
    Compute them in the clean stage from raw if you want them back.
  - The 10-minute horizon is SKIPPED — the smallest buildable horizon is 1 hour.

Idempotent: the table PK is (station_id, timestamp, horizon_minutes) and all
inserts use ON CONFLICT DO NOTHING, so re-running a month is safe.

Usage (run from project root so `citibike` imports):
    python model_training/build_training_features.py --start 2016-01 --end 2021-12
    python model_training/build_training_features.py --start 2026-05 --end 2026-06
    python model_training/build_training_features.py --create-only   # just DDL
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg2
from dateutil.relativedelta import relativedelta

# Make `citibike` importable when run as a script from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citibike.config import DB_CONFIG, HORIZONS_MINUTES  # noqa: E402

DDL_PATH = Path(__file__).resolve().parents[1] / "sql" / "training_features.sql"

# Source of availability: the cleaned, hourly-sampled table (one row/station/hour).
CLEAN_TBL = "station_status_hourly_clean"

# Horizons below this can't be built from hourly data (no sub-hourly snapshots).
MIN_HOURLY_HORIZON_MIN = 60

# The availability gap: no clean rows exist here (the clean stage skipped it), so
# months starting inside this range are skipped — nothing to build.
GAP_START = date(2022, 1, 1)
GAP_END = date(2026, 5, 1)  # exclusive — May 2026 onward has live status data


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def create_table(conn):
    """Run the CREATE TABLE / hypertable DDL (idempotent)."""
    conn.cursor().execute(DDL_PATH.read_text())
    conn.commit()
    print(f"Ensured training_features exists (from {DDL_PATH.name}).")


def sources_for_month(month_start: date):
    """Return the (observed_weather, forecast_weather) tables for a month, or None
    if the month falls in the un-backfillable gap or is excluded (2020).

    The availability source is always CLEAN_TBL (both eras are merged into it);
    only the WEATHER tables still differ by era.
    """
    if GAP_START <= month_start < GAP_END:
        return None  # 2022-April 2026 — no clean rows
    if month_start.year == 2020:
        return None  # COVID anomaly, excluded from training per project spec

    if month_start.year <= 2021:
        return (
            "weather_pre2021_era5_observed",
            "weather_pre2021_gfs_forecast",
        )
    return (
        "weather_post2021_openmeteo_observed",
        "weather_post2021_openmeteo_forecast",
    )


def months_between(start: date, end: date):
    cur = start.replace(day=1)
    last = end.replace(day=1)
    while cur <= last:
        yield cur
        cur += relativedelta(months=1)


def build_base_table(conn, observed_tbl, m_start, m_end):
    """Create a per-month temp table of base rows plus all horizon-INDEPENDENT
    features (current availability, lags, observed weather, static, demand).
    Horizon-dependent columns (target, forecast) are added later.

    Reads straight from CLEAN_TBL — already one row per (station_id, hour), so no
    sampling CTE is needed. Lag lookups use the (station_id, hour DESC) index and
    tolerate gaps by taking the most recent clean row at/before the lag time.
    """
    sql = f"""
    DROP TABLE IF EXISTS _tf_base;
    CREATE TEMP TABLE _tf_base AS
    SELECT
        s.station_id,
        s.hour AS ts,
        s.num_bikes_available,
        s.num_ebikes_available,
        s.num_docks_available,
        s.num_bikes_disabled,

        -- capacity-normalized availability (stationary, comparable across stations).
        -- Normalize by capacity (a fixed positive constant) NOT by the prior count,
        -- so it stays zero-safe when a dock is empty. NULLIF guards capacity = 0.
        -- capacity is frozen into the clean table, so no station_information join.
        s.num_bikes_available::numeric / NULLIF(s.capacity, 0) AS fill_ratio,
        (s.num_bikes_available - lag1.num_bikes_available)::numeric
            / NULLIF(s.capacity, 0) AS fill_ratio_change_1hr,
        roll6.avg_bikes / NULLIF(s.capacity, 0) AS rolling_mean_fill_ratio_6hr,

        -- lag lookups: latest clean snapshot at/just before (ts - interval)
        lag1.num_bikes_available  AS bikes_1hr_ago,
        lag3.num_bikes_available  AS bikes_3hr_ago,
        lag6.num_bikes_available  AS bikes_6hr_ago,
        lag12.num_bikes_available AS bikes_12hr_ago,
        lagyday.num_bikes_available AS bikes_same_hour_yesterday,

        -- rate of change over sub-hourly windows: NOT computable from hourly clean
        -- data. NULL here; compute in the clean stage from raw if you want them.
        NULL::double precision AS rate_of_change_10min,
        NULL::double precision AS rate_of_change_20min,
        NULL::double precision AS rate_of_change_30min,

        -- observed weather, matched on the hour
        w.temperature_2m, w.apparent_temperature, w.precipitation, w.rain,
        w.snowfall, w.wind_speed_10m, w.cloud_cover, w.relative_humidity_2m,

        -- station static (capacity from the clean table; the rest joined by id)
        s.capacity,
        prox.nearest_entrance_dist_m, prox.entrance_count_400m,
        prox.entrance_count_800m, prox.is_within_400m,
        stf.member_ratio, stf.ebike_ratio, stf.station_role,

        -- demand signals
        COALESCE(flow.departures, 0) AS departures_this_hour,
        COALESCE(flow.arrivals, 0)   AS arrivals_this_hour,
        dp.avg_departures AS avg_departures_this_hour_dow,
        dp.avg_arrivals   AS avg_arrivals_this_hour_dow,
        dp.avg_net_flow   AS avg_net_flow_this_hour_dow
    FROM {CLEAN_TBL} s
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {CLEAN_TBL} t
        WHERE t.station_id = s.station_id AND t.hour <= s.hour - INTERVAL '1 hour'
        ORDER BY t.hour DESC LIMIT 1) lag1 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {CLEAN_TBL} t
        WHERE t.station_id = s.station_id AND t.hour <= s.hour - INTERVAL '3 hours'
        ORDER BY t.hour DESC LIMIT 1) lag3 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {CLEAN_TBL} t
        WHERE t.station_id = s.station_id AND t.hour <= s.hour - INTERVAL '6 hours'
        ORDER BY t.hour DESC LIMIT 1) lag6 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {CLEAN_TBL} t
        WHERE t.station_id = s.station_id AND t.hour <= s.hour - INTERVAL '12 hours'
        ORDER BY t.hour DESC LIMIT 1) lag12 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {CLEAN_TBL} t
        WHERE t.station_id = s.station_id AND t.hour <= s.hour - INTERVAL '24 hours'
        ORDER BY t.hour DESC LIMIT 1) lagyday ON true
    -- mean availability over the trailing 6 hours (divided by capacity above)
    LEFT JOIN LATERAL (SELECT avg(num_bikes_available) AS avg_bikes FROM {CLEAN_TBL} t
        WHERE t.station_id = s.station_id
          AND t.hour >  s.hour - INTERVAL '6 hours'
          AND t.hour <= s.hour) roll6 ON true
    LEFT JOIN {observed_tbl} w
        ON w.timestamp = s.hour
    LEFT JOIN citibike_station_subway_proximity prox
        ON prox.citibike_station_id = s.station_id
    LEFT JOIN station_trip_features stf           ON stf.station_id = s.station_id
    LEFT JOIN station_hourly_flow flow
        ON flow.station_id = s.station_id AND flow.hour = s.hour
    LEFT JOIN station_demand_profile dp
        ON dp.station_id = s.station_id
       AND dp.hour_of_day = EXTRACT(HOUR FROM s.hour)
       AND dp.day_of_week = EXTRACT(DOW FROM s.hour)
    WHERE s.hour >= %(m_start)s AND s.hour < %(m_end)s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"m_start": m_start, "m_end": m_end})
        cur.execute("SELECT count(*) FROM _tf_base;")
        n = cur.fetchone()[0]
    conn.commit()
    return n


def insert_horizon(conn, forecast_tbl, horizon, m_start, m_end):
    """Join the per-month base table to the target and horizon-matched forecast
    weather, and append rows for one horizon."""
    lead_hours = max(1, round(horizon / 60))
    sql = f"""
    INSERT INTO training_features (
        station_id, "timestamp", horizon_minutes,
        bikes_available_at_horizon, bike_available_binary,
        hour_of_day, day_of_week, month, season, is_weekend, is_holiday,
        num_bikes_available, num_ebikes_available, num_docks_available, num_bikes_disabled,
        fill_ratio, fill_ratio_change_1hr, rolling_mean_fill_ratio_6hr,
        bikes_1hr_ago, bikes_3hr_ago, bikes_6hr_ago, bikes_12hr_ago,
        bikes_same_hour_yesterday,
        rate_of_change_10min, rate_of_change_20min, rate_of_change_30min,
        temperature_2m, apparent_temperature, precipitation, rain,
        snowfall, wind_speed_10m, cloud_cover, relative_humidity_2m,
        forecast_temperature_2m, forecast_apparent_temperature, forecast_precipitation,
        forecast_rain, forecast_snowfall, forecast_wind_speed_10m,
        forecast_cloud_cover, forecast_relative_humidity_2m,
        capacity, nearest_entrance_dist_m, entrance_count_400m, entrance_count_800m,
        is_within_400m, member_ratio, ebike_ratio, station_role,
        departures_this_hour, arrivals_this_hour,
        avg_departures_this_hour_dow, avg_arrivals_this_hour_dow, avg_net_flow_this_hour_dow
    )
    SELECT
        b.station_id, b.ts, %(horizon)s,
        tgt.num_bikes_available,
        CASE WHEN tgt.num_bikes_available > 0 THEN 1 ELSE 0 END,

        EXTRACT(HOUR FROM b.ts), EXTRACT(DOW FROM b.ts), EXTRACT(MONTH FROM b.ts),
        CASE
            WHEN EXTRACT(MONTH FROM b.ts) IN (12,1,2)  THEN 'winter'
            WHEN EXTRACT(MONTH FROM b.ts) IN (3,4,5)   THEN 'spring'
            WHEN EXTRACT(MONTH FROM b.ts) IN (6,7,8)   THEN 'summer'
            ELSE 'fall'
        END,
        (EXTRACT(DOW FROM b.ts) IN (0,6)),
        FALSE,  -- is_holiday: TODO wire up a holiday calendar (US federal + NYC)

        b.num_bikes_available, b.num_ebikes_available, b.num_docks_available, b.num_bikes_disabled,
        b.fill_ratio, b.fill_ratio_change_1hr, b.rolling_mean_fill_ratio_6hr,
        b.bikes_1hr_ago, b.bikes_3hr_ago, b.bikes_6hr_ago, b.bikes_12hr_ago,
        b.bikes_same_hour_yesterday,
        b.rate_of_change_10min, b.rate_of_change_20min, b.rate_of_change_30min,
        b.temperature_2m, b.apparent_temperature, b.precipitation, b.rain,
        b.snowfall, b.wind_speed_10m, b.cloud_cover, b.relative_humidity_2m,

        f.temperature_2m, f.apparent_temperature, f.precipitation, f.rain,
        f.snowfall, f.wind_speed_10m, f.cloud_cover, f.relative_humidity_2m,

        b.capacity, b.nearest_entrance_dist_m, b.entrance_count_400m, b.entrance_count_800m,
        b.is_within_400m, b.member_ratio, b.ebike_ratio, b.station_role,
        b.departures_this_hour, b.arrivals_this_hour,
        b.avg_departures_this_hour_dow, b.avg_arrivals_this_hour_dow, b.avg_net_flow_this_hour_dow
    FROM _tf_base b
    -- TARGET: the clean snapshot at hour (ts + horizon). Direct hourly lookup with
    -- a small forward tolerance so one missing hour doesn't drop the row. LEFT JOIN
    -- so targets past the month boundary just produce NULL (filtered out below).
    LEFT JOIN LATERAL (
        SELECT num_bikes_available FROM {CLEAN_TBL} t
        WHERE t.station_id = b.station_id
          AND t.hour >= b.ts + INTERVAL '{horizon} minutes'
          AND t.hour <  b.ts + INTERVAL '{horizon} minutes' + INTERVAL '2 hours'
        ORDER BY t.hour ASC LIMIT 1
    ) tgt ON true
    -- FORECAST WEATHER: the forecast for valid_time = hour(ts+horizon), issued
    -- at/before ts, with lead time closest to this horizon.
    LEFT JOIN LATERAL (
        SELECT * FROM {forecast_tbl} fc
        WHERE fc.valid_time = date_trunc('hour', b.ts + INTERVAL '{horizon} minutes')
          AND fc.run_time <= b.ts
        ORDER BY abs(fc.lead_time_hours - {lead_hours}) ASC, fc.run_time DESC
        LIMIT 1
    ) f ON true
    WHERE tgt.num_bikes_available IS NOT NULL
    ON CONFLICT (station_id, "timestamp", horizon_minutes) DO NOTHING;
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"horizon": horizon})
        n = cur.rowcount
    conn.commit()
    return n


def build_month(conn, month_start):
    src = sources_for_month(month_start)
    if src is None:
        print(f"  {month_start:%Y-%m}  SKIP (gap / excluded year)")
        return
    observed_tbl, forecast_tbl = src
    m_end = month_start + relativedelta(months=1)

    n_base = build_base_table(conn, observed_tbl, month_start, m_end)
    print(f"  {month_start:%Y-%m}  base rows: {n_base:,}  (src={CLEAN_TBL})")

    for h in HORIZONS_MINUTES:
        if h < MIN_HOURLY_HORIZON_MIN:
            print(f"      horizon {h:>4}min -> SKIP (sub-hourly; clean table is hourly)")
            continue
        n = insert_horizon(conn, forecast_tbl, h, month_start, m_end)
        print(f"      horizon {h:>4}min -> {n:,} rows inserted")


def parse_month(s: str) -> date:
    return datetime.strptime(s, "%Y-%m").date().replace(day=1)


def main():
    ap = argparse.ArgumentParser(description="Build the training_features table.")
    ap.add_argument("--start", type=parse_month, help="first month, YYYY-MM")
    ap.add_argument("--end", type=parse_month, help="last month, YYYY-MM (inclusive)")
    ap.add_argument("--create-only", action="store_true",
                    help="just run the DDL and exit")
    args = ap.parse_args()

    conn = get_conn()
    try:
        create_table(conn)
        if args.create_only:
            return
        if not (args.start and args.end):
            ap.error("--start and --end are required unless --create-only")

        for m in months_between(args.start, args.end):
            build_month(conn, m)
        print("Done.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

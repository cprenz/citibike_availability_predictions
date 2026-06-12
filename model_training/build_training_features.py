"""Phase 2 — build the training_features table.

Architecture: this script is a thin ORCHESTRATOR. It loops over the training
period one month at a time and runs INSERT ... SELECT statements that compute
every feature INSIDE the database (TimescaleDB). Data is never pulled into
pandas — the 334M-row status archive is far too large for that, and Timescale
is purpose-built for the time-window / lag work.

Per month the builder:
  1. Picks the right source tables (pre-2021 vs post-2021) and SKIPS the
     2022-April 2026 availability gap, where lag features can't be computed.
  2. Samples one snapshot per (station_id, hour)  -> the "base" rows.
  3. Computes lags, observed weather, static, and demand features once.
  4. For each horizon, writes target + horizon-matched forecast weather.

Idempotent: the table PK is (station_id, timestamp, horizon_minutes) and all
inserts use ON CONFLICT DO NOTHING, so re-running a month is safe. This is what
makes the monthly retrain pipeline (Phase 3) just an append.

Usage (run from project root so `citibike` imports):
    python model_training/build_training_features.py --start 2016-01 --end 2021-12
    python model_training/build_training_features.py --start 2026-05 --end 2026-06
    python model_training/build_training_features.py --create-only   # just DDL

Validate on a SMALL window first (e.g. one month) and eyeball the output before
launching the full multi-year backfill — the lag/forecast SQL is the expensive,
correctness-critical part.
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

# One base row per station per this interval. Hourly keeps the table tractable:
# ~1,500 stations * 24h * 30d * 7 horizons ~= 7.5M rows/month. Going finer
# (e.g. '15 minutes') multiplies the row count and the build time accordingly.
SAMPLE_INTERVAL = "1 hour"

# The availability gap: no raw status snapshots exist here, so lag features are
# impossible. Months that start inside this range are skipped entirely.
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
    """Return the (status, observed_weather, forecast_weather) tables for a month,
    or None if the month falls in the un-backfillable gap or is excluded (2020)."""
    if GAP_START <= month_start < GAP_END:
        return None  # 2022-April 2026 — only trip aggregates exist, no snapshots
    if month_start.year == 2020:
        return None  # COVID anomaly, excluded from training per project spec

    if month_start.year <= 2021:
        return (
            "station_status_pre2021",
            "weather_pre2021_era5_observed",
            "weather_pre2021_gfs_forecast",
        )
    return (
        "station_status",
        "weather_post2021_openmeteo_observed",
        "weather_post2021_openmeteo_forecast",
    )


def months_between(start: date, end: date):
    cur = start.replace(day=1)
    last = end.replace(day=1)
    while cur <= last:
        yield cur
        cur += relativedelta(months=1)


def build_base_table(conn, status_tbl, observed_tbl, m_start, m_end):
    """Create a per-month temp table of sampled base rows plus all horizon-
    INDEPENDENT features (current availability, lags, observed weather, static,
    demand). Horizon-dependent columns (target, forecast) are added later.

    NOTE: the lag/rate-of-change SQL below uses LATERAL lookups against the
    (station_id, fetched_at DESC) index. Verify EXPLAIN on a single month before
    a full backfill — this is the hot path over the 334M-row archive.
    """
    sql = f"""
    DROP TABLE IF EXISTS _tf_base;
    CREATE TEMP TABLE _tf_base AS
    WITH sampled AS (
        -- one snapshot per station per SAMPLE_INTERVAL bucket
        SELECT DISTINCT ON (station_id, time_bucket('{SAMPLE_INTERVAL}', fetched_at))
            station_id,
            fetched_at AS ts,
            num_bikes_available,
            num_ebikes_available,
            num_docks_available,
            num_bikes_disabled
        FROM {status_tbl}
        WHERE fetched_at >= %(m_start)s AND fetched_at < %(m_end)s
        ORDER BY station_id,
                 time_bucket('{SAMPLE_INTERVAL}', fetched_at),
                 fetched_at DESC
    )
    SELECT
        s.station_id,
        s.ts,
        s.num_bikes_available,
        s.num_ebikes_available,
        s.num_docks_available,
        s.num_bikes_disabled,

        -- capacity-normalized availability (stationary, comparable across stations).
        -- Normalize by capacity (a fixed positive constant) NOT by the prior count,
        -- so it stays zero-safe when a dock is empty. NULLIF guards capacity = 0.
        s.num_bikes_available::numeric / NULLIF(si.capacity, 0) AS fill_ratio,
        (s.num_bikes_available - lag1.num_bikes_available)::numeric
            / NULLIF(si.capacity, 0) AS fill_ratio_change_1hr,
        roll6.avg_bikes / NULLIF(si.capacity, 0) AS rolling_mean_fill_ratio_6hr,

        -- lag lookups: latest snapshot at/just before (ts - interval)
        lag1.num_bikes_available  AS bikes_1hr_ago,
        lag3.num_bikes_available  AS bikes_3hr_ago,
        lag6.num_bikes_available  AS bikes_6hr_ago,
        lag12.num_bikes_available AS bikes_12hr_ago,
        lagyday.num_bikes_available AS bikes_same_hour_yesterday,

        -- rate of change (bikes per minute) over short windows
        (s.num_bikes_available - r10.num_bikes_available) / 10.0 AS rate_of_change_10min,
        (s.num_bikes_available - r20.num_bikes_available) / 20.0 AS rate_of_change_20min,
        (s.num_bikes_available - r30.num_bikes_available) / 30.0 AS rate_of_change_30min,

        -- observed weather, truncated to the hour
        w.temperature_2m, w.apparent_temperature, w.precipitation, w.rain,
        w.snowfall, w.wind_speed_10m, w.cloud_cover, w.relative_humidity_2m,

        -- station static
        si.capacity,
        prox.nearest_entrance_dist_m, prox.entrance_count_400m,
        prox.entrance_count_800m, prox.is_within_400m,
        stf.member_ratio, stf.ebike_ratio, stf.station_role,

        -- demand signals
        COALESCE(flow.departures, 0) AS departures_this_hour,
        COALESCE(flow.arrivals, 0)   AS arrivals_this_hour,
        dp.avg_departures AS avg_departures_this_hour_dow,
        dp.avg_arrivals   AS avg_arrivals_this_hour_dow,
        dp.avg_net_flow   AS avg_net_flow_this_hour_dow
    FROM sampled s
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {status_tbl} t
        WHERE t.station_id = s.station_id AND t.fetched_at <= s.ts - INTERVAL '1 hour'
        ORDER BY t.fetched_at DESC LIMIT 1) lag1 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {status_tbl} t
        WHERE t.station_id = s.station_id AND t.fetched_at <= s.ts - INTERVAL '3 hours'
        ORDER BY t.fetched_at DESC LIMIT 1) lag3 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {status_tbl} t
        WHERE t.station_id = s.station_id AND t.fetched_at <= s.ts - INTERVAL '6 hours'
        ORDER BY t.fetched_at DESC LIMIT 1) lag6 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {status_tbl} t
        WHERE t.station_id = s.station_id AND t.fetched_at <= s.ts - INTERVAL '12 hours'
        ORDER BY t.fetched_at DESC LIMIT 1) lag12 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {status_tbl} t
        WHERE t.station_id = s.station_id AND t.fetched_at <= s.ts - INTERVAL '24 hours'
        ORDER BY t.fetched_at DESC LIMIT 1) lagyday ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {status_tbl} t
        WHERE t.station_id = s.station_id AND t.fetched_at <= s.ts - INTERVAL '10 minutes'
        ORDER BY t.fetched_at DESC LIMIT 1) r10 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {status_tbl} t
        WHERE t.station_id = s.station_id AND t.fetched_at <= s.ts - INTERVAL '20 minutes'
        ORDER BY t.fetched_at DESC LIMIT 1) r20 ON true
    LEFT JOIN LATERAL (SELECT num_bikes_available FROM {status_tbl} t
        WHERE t.station_id = s.station_id AND t.fetched_at <= s.ts - INTERVAL '30 minutes'
        ORDER BY t.fetched_at DESC LIMIT 1) r30 ON true
    -- mean availability over the trailing 6 hours (divided by capacity above)
    LEFT JOIN LATERAL (SELECT avg(num_bikes_available) AS avg_bikes FROM {status_tbl} t
        WHERE t.station_id = s.station_id
          AND t.fetched_at >  s.ts - INTERVAL '6 hours'
          AND t.fetched_at <= s.ts) roll6 ON true
    LEFT JOIN {observed_tbl} w
        ON w.timestamp = date_trunc('hour', s.ts)
    LEFT JOIN station_information si              ON si.station_id = s.station_id
    LEFT JOIN citibike_station_subway_proximity prox
        ON prox.citibike_station_id = s.station_id
    LEFT JOIN station_trip_features stf           ON stf.station_id = s.station_id
    LEFT JOIN station_hourly_flow flow
        ON flow.station_id = s.station_id AND flow.hour = date_trunc('hour', s.ts)
    LEFT JOIN station_demand_profile dp
        ON dp.station_id = s.station_id
       AND dp.hour_of_day = EXTRACT(HOUR FROM s.ts)
       AND dp.day_of_week = EXTRACT(DOW FROM s.ts);
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"m_start": m_start, "m_end": m_end})
        cur.execute("SELECT count(*) FROM _tf_base;")
        n = cur.fetchone()[0]
    conn.commit()
    return n


def insert_horizon(conn, status_tbl, forecast_tbl, horizon, m_start, m_end):
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
    -- TARGET: nearest snapshot at/after (ts + horizon). LEFT JOIN so rows whose
    -- target falls past the month boundary still build (target NULL -> filtered
    -- out below, since a row with no label is useless for training).
    LEFT JOIN LATERAL (
        SELECT num_bikes_available FROM {status_tbl} t
        WHERE t.station_id = b.station_id
          AND t.fetched_at >= b.ts + INTERVAL '{horizon} minutes'
          AND t.fetched_at <  b.ts + INTERVAL '{horizon} minutes' + INTERVAL '30 minutes'
        ORDER BY t.fetched_at ASC LIMIT 1
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
    status_tbl, observed_tbl, forecast_tbl = src
    m_end = month_start + relativedelta(months=1)

    n_base = build_base_table(conn, status_tbl, observed_tbl, month_start, m_end)
    print(f"  {month_start:%Y-%m}  base rows: {n_base:,}  (src={status_tbl})")

    for h in HORIZONS_MINUTES:
        n = insert_horizon(conn, status_tbl, forecast_tbl, h, month_start, m_end)
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

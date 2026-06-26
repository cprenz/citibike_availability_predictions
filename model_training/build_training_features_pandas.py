"""Phase 2 (feature stage, Option B) — build training_features in pandas.

Replaces the slow SQL LATERAL builder (~40 min / two months) with vectorized
pandas shift/rolling on a complete hourly grid. Same source table, same output
columns, ~30-100x faster.

    station_status_hourly_clean  ->  pandas shift/rolling/merge  ->  training_features

I kept the SQL builder (build_training_features.py) as a reference and fallback;
this file is the one actually used for backfill and monthly retraining.

Two deliberate differences from the SQL builder:
  - Lags and targets are EXACT-KEY on the hourly grid (NaN at a gap), vs the SQL's
    fuzzy at/before lookup. The two agree everywhere except genuine hourly gaps.
  - is_holiday is still FALSE (matches the SQL stub). Wiring a real holiday calendar
    is a separate build item.

The never-built stub columns (bikes_same_hour_same_weekday_4wk_avg, emptying_frequency,
etc.) are omitted from the INSERT so they land as NULL — same as the SQL builder.

Idempotent: COPY into a TEMP staging table then INSERT ... ON CONFLICT DO NOTHING
on the (station_id, timestamp, horizon_minutes) PK. Re-running a month is safe
without --fast; --fast skips the staging table and is only safe after a TRUNCATE.

Usage (run from project root so `citibike` imports):
    python model_training/build_training_features_pandas.py --start 2026-05 --end 2026-06
    python model_training/build_training_features_pandas.py --start 2016-01 --end 2021-12
    python model_training/build_training_features_pandas.py --create-only   # just DDL

    # Write to a scratch table for side-by-side validation against the SQL builder:
    python model_training/build_training_features_pandas.py \
        --start 2026-05 --end 2026-06 --table training_features_pandas
"""

import argparse
import io
import math
import multiprocessing
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from dateutil.relativedelta import relativedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citibike.config import DB_CONFIG, HORIZONS_MINUTES  # noqa: E402

DDL_PATH = Path(__file__).resolve().parents[1] / "sql" / "training_features.sql"

CLEAN_TBL = "station_status_hourly_clean"

MIN_HOURLY_HORIZON_MIN = 60  # can't build sub-hourly horizons from hourly data

# Months inside this range have no source data — skip them.
GAP_START = date(2022, 1, 1)
GAP_END = date(2026, 5, 1)  # exclusive — May 2026 onward has live status data

# 24h back feeds the longest lag (same hour yesterday); 48h forward feeds the longest
# target horizon (2880 min) so end-of-month rows still get a target from next month.
BACK_BUFFER_HOURS = 24
FWD_BUFFER_HOURS = max(HORIZONS_MINUTES) // 60  # 2880min -> 48h

# Column order must match the SQL builder's INSERT exactly so the two builders'
# rows are directly comparable. Stub columns not listed here land as NULL.
INSERT_COLUMNS = [
    "station_id", "timestamp", "horizon_minutes",
    "bikes_available_at_horizon", "bike_available_binary",
    "hour_of_day", "day_of_week", "month", "season", "is_weekend", "is_holiday",
    "num_bikes_available", "num_ebikes_available", "num_docks_available", "num_bikes_disabled",
    "num_ebikes_was_null", "num_bikes_disabled_was_null",
    "fill_ratio", "fill_ratio_change_1hr", "rolling_mean_fill_ratio_6hr",
    "bikes_1hr_ago", "bikes_3hr_ago", "bikes_6hr_ago", "bikes_12hr_ago",
    "bikes_same_hour_yesterday",
    "change_bikes_1hr", "change_bikes_3hr", "change_bikes_6hr", "change_bikes_12hr",
    "change_ebikes_1hr", "change_ebikes_3hr", "change_ebikes_6hr", "change_ebikes_12hr",
    "change_classic_1hr", "change_classic_3hr", "change_classic_6hr", "change_classic_12hr",
    "rate_of_change_10min", "rate_of_change_20min", "rate_of_change_30min",
    "temperature_2m", "apparent_temperature", "precipitation", "rain",
    "snowfall", "wind_speed_10m", "cloud_cover", "relative_humidity_2m",
    "forecast_temperature_2m", "forecast_apparent_temperature", "forecast_precipitation",
    "forecast_rain", "forecast_snowfall", "forecast_wind_speed_10m",
    "forecast_cloud_cover", "forecast_relative_humidity_2m",
    "capacity", "nearest_entrance_dist_m", "entrance_count_400m", "entrance_count_800m",
    "is_within_400m", "member_ratio", "ebike_ratio", "station_role",
    "departures_this_hour", "arrivals_this_hour",
    "avg_departures_this_hour_dow", "avg_arrivals_this_hour_dow", "avg_net_flow_this_hour_dow",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos", "month_sin", "month_cos",
    "cumulative_expected_net_flow_1hr", "cumulative_expected_net_flow_3hr",
    "cumulative_expected_net_flow_6hr", "cumulative_expected_net_flow_12hr",
    "cumulative_expected_net_flow_24hr",
    "net_flow_1hr", "net_flow_3hr", "net_flow_6hr",
]

# Cast to nullable Int64 before COPY so to_csv writes "31"/"\N", never "31.0".
# Postgres rejects "31.0" for an INTEGER column — the same trap that hit the clean stage.
INT64_COLUMNS = [
    "horizon_minutes", "bikes_available_at_horizon", "bike_available_binary",
    "hour_of_day", "day_of_week", "month",
    "num_bikes_available", "num_ebikes_available", "num_docks_available", "num_bikes_disabled",
    "bikes_1hr_ago", "bikes_3hr_ago", "bikes_6hr_ago", "bikes_12hr_ago",
    "bikes_same_hour_yesterday",
    "change_bikes_1hr", "change_bikes_3hr", "change_bikes_6hr", "change_bikes_12hr",
    "change_ebikes_1hr", "change_ebikes_3hr", "change_ebikes_6hr", "change_ebikes_12hr",
    "change_classic_1hr", "change_classic_3hr", "change_classic_6hr", "change_classic_12hr",
    "capacity", "entrance_count_400m", "entrance_count_800m",
    "departures_this_hour", "arrivals_this_hour",
]

# "boolean" dtype serializes NA as \N in to_csv, which COPY reads as NULL.
BOOL_COLUMNS = ["is_weekend", "is_holiday", "is_within_400m",
                "num_ebikes_was_null", "num_bikes_disabled_was_null"]

WEATHER_COLS = [
    "temperature_2m", "apparent_temperature", "precipitation", "rain",
    "snowfall", "wind_speed_10m", "cloud_cover", "relative_humidity_2m",
]


_CUMFLOW_HORIZONS_H = [1, 3, 6, 12, 24]


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def build_cumflow_lookup(conn) -> pd.DataFrame:
    """Cumulative expected net flow for every (station_id, hour_of_day, day_of_week).

    Loads station_demand_profile in both namespaces — modern UUID (via short_name join)
    and legacy integer (normalized '116.0' -> '116') — then deduplicates and builds
    prefix sums so any H-hour window is an O(1) lookup.
    """
    demand_modern = pd.read_sql(
        "SELECT si.station_id, d.hour_of_day, d.day_of_week, "
        "COALESCE(d.avg_net_flow, 0.0) AS avg_net_flow "
        "FROM station_demand_profile d "
        "JOIN station_information si ON si.short_name = d.station_id;",
        conn)
    demand_legacy = pd.read_sql(
        "SELECT station_id, hour_of_day, day_of_week, "
        "COALESCE(avg_net_flow, 0.0) AS avg_net_flow "
        "FROM station_demand_profile;",
        conn)
    demand_legacy["station_id"] = demand_legacy["station_id"].str.replace(
        r"\.0$", "", regex=True)
    demand = (pd.concat([demand_modern, demand_legacy], ignore_index=True)
                .drop_duplicates(["station_id", "hour_of_day", "day_of_week"]))
    demand["week_hour"] = (demand["day_of_week"].astype(int) * 24
                           + demand["hour_of_day"].astype(int))
    piv = (demand.pivot_table(index="station_id", columns="week_hour",
                               values="avg_net_flow", aggfunc="first",
                               fill_value=0.0)
                 .reindex(columns=range(168), fill_value=0.0))
    mat  = piv.values
    mat2 = np.hstack([mat, mat])       # circular wrap-around
    cs   = np.cumsum(mat2, axis=1)
    station_ids = piv.index.values
    n = len(station_ids)
    blocks = []
    for h in range(168):
        rec: dict = {
            "station_id":  station_ids,
            "hour_of_day": np.full(n, h % 24, dtype=np.int16),
            "day_of_week": np.full(n, h // 24, dtype=np.int16),
        }
        for H in _CUMFLOW_HORIZONS_H:
            rec[f"cumulative_expected_net_flow_{H}hr"] = cs[:, h + H] - cs[:, h]
        blocks.append(pd.DataFrame(rec))
    return pd.concat(blocks, ignore_index=True)


DEFAULT_TABLE = "training_features"


def create_table(conn, table: str = DEFAULT_TABLE):
    """Ensure the target table exists (idempotent).

    training_features is always created from the DDL file. A scratch table
    (e.g. training_features_pandas for side-by-side validation) is created as
    LIKE training_features INCLUDING ALL so it has the same schema, PK, and indexes.
    """
    cur = conn.cursor()
    cur.execute(DDL_PATH.read_text())  # always ensure canonical training_features
    if table != DEFAULT_TABLE:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            f"(LIKE {DEFAULT_TABLE} INCLUDING ALL);"
        )
        cur.execute(
            f"SELECT create_hypertable('{table}', 'timestamp', if_not_exists => TRUE);"
        )
    conn.commit()
    print(f"Ensured target table {table} exists.")


def weather_sources_for_month(month_start: date):
    """Return (observed_tbl, forecast_tbl) for a month, or None if it's in the gap / 2020."""
    if GAP_START <= month_start < GAP_END:
        return None
    if month_start.year == 2020:
        return None
    if month_start.year <= 2020:
        return ("weather_pre2021_era5_observed", "weather_pre2021_gfs_forecast")
    return ("weather_post2021_openmeteo_observed", "weather_post2021_openmeteo_forecast")


def months_between(start: date, end: date):
    cur = start.replace(day=1)
    last = end.replace(day=1)
    while cur <= last:
        yield cur
        cur += relativedelta(months=1)


_SEASON_BY_MONTH = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "fall", 10: "fall", 11: "fall",
}


def season_of(month: pd.Series) -> pd.Series:
    """Map month number -> season string (matches the SQL builder's CASE)."""
    return month.map(_SEASON_BY_MONTH)


# --- side tables (static across months; loaded once) -----------------------------

def _normalize_legacy_ids(s: pd.Series) -> pd.Series:
    """Strip the float suffix from legacy IDs: '116.0' -> '116'. Modern short-names
    like '6197.08' are unaffected — they don't end in '.0'."""
    return s.str.replace(r"\.0$", "", regex=True)


def load_legacy_capacity(conn) -> pd.Series:
    """Proxy capacity for pre-2021 legacy stations.

    station_information only has modern UUIDs, so the clean stage wrote capacity=NULL
    for all pre-2021 rows. I derive it from the clean table itself: MAX(bikes+docks)
    per station, pre-2022. Much faster than going back to the 334M-row raw archive.
    """
    df = pd.read_sql(
        "SELECT station_id, "
        "MAX(num_bikes_available + num_docks_available) AS capacity "
        "FROM station_status_hourly_clean WHERE hour < '2022-01-01' "
        "GROUP BY station_id;",
        conn)
    return df.set_index("station_id")["capacity"]


def load_static_tables(conn):
    """Load the small, month-independent side tables once: subway proximity, trip features,
    and the demand profile.

    Trip and demand are loaded in two passes and concatenated:
      modern — via station_information.short_name -> UUID (matches post-2021 clean rows)
      legacy — raw station_id normalized '116.0' -> '116' (matches pre-2021 clean rows)
    The two ID spaces are disjoint so no real duplicates arise after dedup.

    Proximity stays UUID-only — no historical lat/lon exists for legacy stations, so
    proximity stays NULL for the pre-2021 era (structural, not a data gap).
    """
    prox = pd.read_sql(
        "SELECT citibike_station_id AS station_id, nearest_entrance_dist_m, "
        "entrance_count_400m, entrance_count_800m, is_within_400m "
        "FROM citibike_station_subway_proximity;", conn)

    trip_modern = pd.read_sql(
        "SELECT si.station_id, t.member_ratio, t.ebike_ratio, t.station_role "
        "FROM station_trip_features t "
        "JOIN station_information si ON si.short_name = t.station_id;", conn)
    trip_legacy = pd.read_sql(
        "SELECT station_id, member_ratio, ebike_ratio, station_role "
        "FROM station_trip_features;", conn)
    trip_legacy["station_id"] = _normalize_legacy_ids(trip_legacy["station_id"])
    trip_legacy = trip_legacy.drop_duplicates("station_id")
    trip = (pd.concat([trip_modern, trip_legacy], ignore_index=True)
              .drop_duplicates("station_id"))

    demand_modern = pd.read_sql(
        "SELECT si.station_id, d.hour_of_day, d.day_of_week, "
        "d.avg_departures AS avg_departures_this_hour_dow, "
        "d.avg_arrivals   AS avg_arrivals_this_hour_dow, "
        "d.avg_net_flow   AS avg_net_flow_this_hour_dow "
        "FROM station_demand_profile d "
        "JOIN station_information si ON si.short_name = d.station_id;", conn)
    demand_legacy = pd.read_sql(
        "SELECT station_id, hour_of_day, day_of_week, "
        "avg_departures AS avg_departures_this_hour_dow, "
        "avg_arrivals   AS avg_arrivals_this_hour_dow, "
        "avg_net_flow   AS avg_net_flow_this_hour_dow "
        "FROM station_demand_profile;", conn)
    demand_legacy["station_id"] = _normalize_legacy_ids(demand_legacy["station_id"])
    demand_legacy = demand_legacy.drop_duplicates(
        ["station_id", "hour_of_day", "day_of_week"])
    demand = (pd.concat([demand_modern, demand_legacy], ignore_index=True)
                .drop_duplicates(["station_id", "hour_of_day", "day_of_week"]))

    cumflow = build_cumflow_lookup(conn)
    return prox, trip, demand, cumflow


def load_clean_window(conn, w_start, w_end) -> pd.DataFrame:
    """Pull clean hourly rows for [w_start, w_end) — the month plus back/forward buffers."""
    sql = f"""
        SELECT station_id, hour, capacity,
               num_bikes_available, num_ebikes_available,
               num_docks_available, num_bikes_disabled
        FROM {CLEAN_TBL}
        WHERE hour >= %(w_start)s AND hour < %(w_end)s;
    """
    df = pd.read_sql(sql, conn, params={"w_start": w_start, "w_end": w_end})
    df["hour"] = pd.to_datetime(df["hour"], utc=True)
    return df


def load_observed_weather(conn, observed_tbl, w_start, w_end) -> pd.DataFrame:
    cols = ", ".join(WEATHER_COLS)
    sql = f"""
        SELECT "timestamp" AS hour, {cols}
        FROM {observed_tbl}
        WHERE "timestamp" >= %(w_start)s AND "timestamp" < %(w_end)s;
    """
    df = pd.read_sql(sql, conn, params={"w_start": w_start, "w_end": w_end})
    df["hour"] = pd.to_datetime(df["hour"], utc=True)
    return df


def load_flow(conn, w_start, w_end) -> pd.DataFrame:
    """Load hourly flow in both namespaces (modern UUID + legacy integer) and combine."""
    flow_modern = pd.read_sql("""
        SELECT si.station_id, f.hour, f.departures, f.arrivals
        FROM station_hourly_flow f
        JOIN station_information si ON si.short_name = f.station_id
        WHERE f.hour >= %(w_start)s AND f.hour < %(w_end)s;
    """, conn, params={"w_start": w_start, "w_end": w_end})
    flow_legacy = pd.read_sql("""
        SELECT station_id, hour, departures, arrivals
        FROM station_hourly_flow
        WHERE hour >= %(w_start)s AND hour < %(w_end)s;
    """, conn, params={"w_start": w_start, "w_end": w_end})
    flow_legacy["station_id"] = _normalize_legacy_ids(flow_legacy["station_id"])
    flow = (pd.concat([flow_modern, flow_legacy], ignore_index=True)
              .drop_duplicates(["station_id", "hour"]))
    flow["hour"] = pd.to_datetime(flow["hour"], utc=True)
    return flow


def load_forecast(conn, forecast_tbl, w_start, w_end) -> pd.DataFrame:
    """Forecast runs whose valid_time falls in the target window.

    Bounding on valid_time (not run_time) ensures every target hour has a forecast available.
    """
    cols = ", ".join(WEATHER_COLS)
    sql = f"""
        SELECT run_time, valid_time, lead_time_hours, {cols}
        FROM {forecast_tbl}
        WHERE valid_time >= %(w_start)s AND valid_time < %(w_end)s;
    """
    fc = pd.read_sql(sql, conn, params={"w_start": w_start, "w_end": w_end})
    # pd.read_sql can return TIMESTAMPTZ as object dtype; force datetime64[ns,UTC]
    # so downstream merges on these keys don't hit an object-vs-datetime mismatch.
    fc["valid_time"] = pd.to_datetime(fc["valid_time"], utc=True)
    fc["run_time"] = pd.to_datetime(fc["run_time"], utc=True)
    return fc


def forecast_for_horizon(fc: pd.DataFrame, lead_hours: int) -> pd.DataFrame:
    """Collapse the forecast table to one row per valid_time for this horizon.

    Reproduces the SQL builder's LATERAL: among runs issued at/before the prediction
    time (run_time <= ts), pick the one whose lead is closest to lead_hours. Because
    ts = valid_time - horizon, "run_time <= ts" is equivalent to "lead_time_hours >= lead_hours".
    So: filter lead >= lead_hours, take the minimum lead per valid_time (= most recent run).
    """
    c = fc[fc["lead_time_hours"] >= lead_hours]
    c = c.sort_values(["valid_time", "lead_time_hours"]).groupby("valid_time", as_index=False).first()
    rename = {col: f"forecast_{col}" for col in WEATHER_COLS}
    return c.rename(columns=rename)[["valid_time"] + list(rename.values())]


def build_grid(clean: pd.DataFrame, w_start, w_end) -> pd.DataFrame:
    """Reindex every station to a complete hourly grid so gaps become explicit NaN
    and .shift() lags/targets are exact-key lookups rather than fuzzy searches."""
    full_index = pd.date_range(w_start, w_end, freq="h", inclusive="left", tz="UTC")
    stations = clean["station_id"].unique()
    mi = pd.MultiIndex.from_product([stations, full_index], names=["station_id", "hour"])
    grid = clean.set_index(["station_id", "hour"]).reindex(mi)
    # capacity is frozen per station; carry it onto filled rows so real month rows
    # divided in fill_ratio always have a denominator even if their own hour existed
    # only as a gap-fill (real rows already carry it; this is belt-and-suspenders).
    # Carry capacity onto gap-fill rows — real rows already have it; this covers
    # the rare case where a station's only capacity row is in the buffer.
    grid["capacity"] = grid.groupby(level=0)["capacity"].transform("max")
    return grid.sort_index()


def build_base_features(grid: pd.DataFrame) -> pd.DataFrame:
    """Compute all horizon-independent features via grouped shifts/rolling.

    Returns a frame indexed (station_id, hour). Horizon-specific targets and
    forecast weather are added later in the per-horizon loop.
    """
    g = grid.groupby(level=0)
    bikes = grid["num_bikes_available"]
    ebikes = grid["num_ebikes_available"]
    classic = bikes - ebikes
    cap = grid["capacity"]

    out = pd.DataFrame(index=grid.index)
    out["num_bikes_available"] = bikes
    # NULL here means the column wasn't tracked in this era, not a data gap.
    # 0 is the right fill — pre-ebike stations had zero ebikes.
    out["num_ebikes_was_null"] = ebikes.isna()
    out["num_ebikes_available"] = ebikes.fillna(0)
    out["num_docks_available"] = grid["num_docks_available"]
    disabled = grid["num_bikes_disabled"]
    out["num_bikes_disabled_was_null"] = disabled.isna()
    out["num_bikes_disabled"] = disabled.fillna(0)
    out["capacity"] = cap

    # Lags are exact-key on the complete grid — NaN where a station had an hourly gap.
    b1, b3, b6, b12 = g["num_bikes_available"].shift(1), g["num_bikes_available"].shift(3), \
        g["num_bikes_available"].shift(6), g["num_bikes_available"].shift(12)
    e1, e3, e6, e12 = g["num_ebikes_available"].shift(1), g["num_ebikes_available"].shift(3), \
        g["num_ebikes_available"].shift(6), g["num_ebikes_available"].shift(12)
    out["bikes_1hr_ago"], out["bikes_3hr_ago"] = b1, b3
    out["bikes_6hr_ago"], out["bikes_12hr_ago"] = b6, b12
    out["bikes_same_hour_yesterday"] = g["num_bikes_available"].shift(24)

    # NULLIF(capacity, 0): cast to float first so nullable-Int64 <NA> becomes np.nan,
    # then mask 0 -> NaN. Zero/absent-capacity stations get NaN fill_ratio, matching the SQL builder.
    cap_safe = cap.astype("float64")
    cap_safe = cap_safe.mask(cap_safe == 0)
    out["fill_ratio"] = bikes / cap_safe
    out["fill_ratio_change_1hr"] = (bikes - b1) / cap_safe
    roll6 = g["num_bikes_available"].rolling(6, min_periods=1).mean().reset_index(level=0, drop=True)
    out["rolling_mean_fill_ratio_6hr"] = roll6 / cap_safe

    # Lagged classic is derived from lagged total/ebike pairs, not from lagged classic
    # directly — avoids double-counting the GBFS total/ebike overlap.
    cl1 = b1 - e1
    cl3 = b3 - e3
    cl6 = b6 - e6
    cl12 = b12 - e12
    out["change_bikes_1hr"], out["change_bikes_3hr"] = bikes - b1, bikes - b3
    out["change_bikes_6hr"], out["change_bikes_12hr"] = bikes - b6, bikes - b12
    out["change_ebikes_1hr"], out["change_ebikes_3hr"] = ebikes - e1, ebikes - e3
    out["change_ebikes_6hr"], out["change_ebikes_12hr"] = ebikes - e6, ebikes - e12
    out["change_classic_1hr"], out["change_classic_3hr"] = classic - cl1, classic - cl3
    out["change_classic_6hr"], out["change_classic_12hr"] = classic - cl6, classic - cl12

    # Sub-hourly rates can't be computed from hourly data — deferred per 2026-06-15 decision.
    out["rate_of_change_10min"] = pd.NA
    out["rate_of_change_20min"] = pd.NA
    out["rate_of_change_30min"] = pd.NA
    return out


def add_targets(base: pd.DataFrame, grid: pd.DataFrame, horizons_hours):
    """Build one target column per horizon: bikes at (hour + horizon), exact-key on the grid."""
    g = grid.groupby(level=0)["num_bikes_available"]
    targets = {}
    for h in horizons_hours:
        targets[h] = g.shift(-h)  # value h hours ahead
    return targets


def assemble(conn, month_start: date, observed_tbl, forecast_tbl,
             prox, trip, demand, cumflow, legacy_capacity=None) -> pd.DataFrame:
    """Build the long-form feature frame for one month (one row per station x hour x horizon),
    ready to COPY into training_features."""
    m_end = month_start + relativedelta(months=1)
    w_start = pd.Timestamp(month_start, tz="UTC") - pd.Timedelta(hours=BACK_BUFFER_HOURS)
    w_end = pd.Timestamp(m_end, tz="UTC") + pd.Timedelta(hours=FWD_BUFFER_HOURS)
    m_start_ts = pd.Timestamp(month_start, tz="UTC")
    m_end_ts = pd.Timestamp(m_end, tz="UTC")

    t0 = time.time()
    def elapsed(): return f"{time.time()-t0:.1f}s"

    clean = load_clean_window(conn, w_start, w_end)
    if clean.empty:
        return pd.DataFrame()
    # station_information has no legacy integer IDs, so the clean stage wrote
    # capacity=NULL for all pre-2021 rows. Fill from the precomputed MAX(bikes+docks) proxy.
    if legacy_capacity is not None:
        null_mask = clean["capacity"].isna()
        if null_mask.any():
            clean.loc[null_mask, "capacity"] = (
                clean.loc[null_mask, "station_id"].map(legacy_capacity))
    print(f"    load_clean {elapsed()} ({len(clean):,} rows)", flush=True)

    grid = build_grid(clean, w_start, w_end)
    print(f"    build_grid {elapsed()}", flush=True)

    base = build_base_features(grid)
    print(f"    base_features {elapsed()}", flush=True)

    horizons = [h for h in HORIZONS_MINUTES if h >= MIN_HOURLY_HORIZON_MIN]
    horizons_hours = [h // 60 for h in horizons]
    targets = add_targets(base, grid, horizons_hours)

    # Emit only real observations — the complete hourly grid has gap-fill slots that were
    # never actually observed. notna() on num_bikes_available identifies real rows because
    # the clean stage guarantees 0 NULLs there. Buffer rows (back 24h / forward 48h)
    # only ever feed lags and targets; they never end up in the output.
    hour_idx = base.index.get_level_values("hour")
    emit = ((hour_idx >= m_start_ts) & (hour_idx < m_end_ts)
            & base["num_bikes_available"].notna().to_numpy())
    base = base[emit].reset_index()               # station_id, hour, features...
    targets = {h: t[emit].reset_index(drop=True) for h, t in targets.items()}
    print(f"    emit_filter {elapsed()} ({len(base):,} rows)", flush=True)

    # Postgres EXTRACT(DOW) is Sunday=0..Saturday=6; pandas dayofweek is Monday=0.
    # The (dayofweek+1)%7 conversion matches what's already in training_features.
    hr = base["hour"]
    base["hour_of_day"] = hr.dt.hour
    pg_dow = (hr.dt.dayofweek + 1) % 7      # pandas Mon=0 -> Postgres Sun=0
    base["day_of_week"] = pg_dow
    base["month"] = hr.dt.month
    base["season"] = season_of(base["month"])
    base["is_weekend"] = pg_dow.isin([0, 6])
    base["is_holiday"] = False              # TODO: wire real US-federal + NYC calendar

    # Cyclical encodings so the model knows hour 23 and hour 0 are adjacent.
    base["hour_sin"]  = np.sin(2 * math.pi * base["hour_of_day"] / 24)
    base["hour_cos"]  = np.cos(2 * math.pi * base["hour_of_day"] / 24)
    base["dow_sin"]   = np.sin(2 * math.pi * base["day_of_week"] / 7)
    base["dow_cos"]   = np.cos(2 * math.pi * base["day_of_week"] / 7)
    base["month_sin"] = np.sin(2 * math.pi * base["month"] / 12)
    base["month_cos"] = np.cos(2 * math.pi * base["month"] / 12)

    # Weather, static features, and demand signals are the same for every horizon.
    obs = load_observed_weather(conn, observed_tbl, w_start, w_end)
    base = base.merge(obs, on="hour", how="left")
    print(f"    merge_weather {elapsed()}", flush=True)
    base = base.merge(prox, on="station_id", how="left")
    base = base.merge(trip, on="station_id", how="left")

    # Back buffer covers 24h before month start — more than enough for the 6h net_flow lag.
    flow = load_flow(conn, w_start, m_end_ts)
    print(f"    load_flow {elapsed()} ({len(flow):,} rows)", flush=True)
    # Current-hour flow: restrict to target month before merging (buffer rows aren't emitted).
    flow_current = flow[flow["hour"] >= m_start_ts]
    base = base.merge(flow_current, on=["station_id", "hour"], how="left")
    # Stations with no trips this hour get 0 — COALESCE equivalent.
    base["departures_this_hour"] = pd.to_numeric(base["departures"], errors="coerce").fillna(0)
    base["arrivals_this_hour"] = pd.to_numeric(base["arrivals"], errors="coerce").fillna(0)
    base = base.merge(
        demand, left_on=["station_id", "hour_of_day", "day_of_week"],
        right_on=["station_id", "hour_of_day", "day_of_week"], how="left")
    print(f"    merge_demand {elapsed()}", flush=True)

    # Cumulative expected net flow — precomputed lookup keyed by (station, time-of-week).
    base = base.merge(
        cumflow, on=["station_id", "hour_of_day", "day_of_week"], how="left")

    # Net-flow momentum lags — flow is already loaded with the back buffer above.
    flow_nf = flow.assign(
        net_flow=flow["arrivals"] - flow["departures"]
    )[["station_id", "hour", "net_flow"]]
    for lag_h in [1, 3, 6]:
        col = f"net_flow_{lag_h}hr"
        lag = flow_nf.copy().rename(columns={"net_flow": col, "hour": "hour_lag"})
        lag["hour_lag"] = lag["hour_lag"] + pd.Timedelta(hours=lag_h)
        base = base.merge(
            lag.rename(columns={"hour_lag": "hour"}),
            on=["station_id", "hour"], how="left")
    print(f"    merge_tier1 {elapsed()}", flush=True)

    # Explode to long form — one block per horizon with its target and matched forecast weather.
    fc_raw = load_forecast(conn, forecast_tbl, w_start, w_end)
    print(f"    load_forecast {elapsed()}", flush=True)
    blocks = []
    for h_min, h_hr in zip(horizons, horizons_hours):
        blk = base.copy()
        blk["horizon_minutes"] = h_min
        tgt = targets[h_hr]
        blk["bikes_available_at_horizon"] = tgt.values
        blk = blk[blk["bikes_available_at_horizon"].notna()].copy()
        blk["bike_available_binary"] = (blk["bikes_available_at_horizon"] > 0).astype("Int64")

        # forecast weather: valid_time = hour + horizon, matched per horizon
        lead_hours = max(1, round(h_min / 60))
        fc_h = forecast_for_horizon(fc_raw, lead_hours)
        blk["valid_time"] = blk["hour"] + pd.Timedelta(hours=h_hr)
        blk = blk.merge(fc_h, on="valid_time", how="left")
        blocks.append(blk)

    df = pd.concat(blocks, ignore_index=True)
    print(f"    concat {elapsed()} ({len(df):,} rows)", flush=True)
    df = df.rename(columns={"hour": "timestamp"})
    return df


def copy_into_features(conn, df: pd.DataFrame, table: str = DEFAULT_TABLE,
                       fast: bool = False) -> int:
    """COPY assembled rows into the target table.

    fast=False: COPY -> temp staging -> INSERT ON CONFLICT DO NOTHING. Safe for
    incremental monthly retrain runs where rows from a prior run may already exist.
    fast=True: COPY directly with synchronous_commit=off. Use after TRUNCATE only —
    if the process dies mid-run, TRUNCATE and re-run from scratch.
    """
    if df.empty:
        return 0

    out = df.copy()
    for c in INT64_COLUMNS:
        if c in out:
            out[c] = out[c].astype("Int64")
    for c in BOOL_COLUMNS:
        if c in out:
            out[c] = out[c].astype("boolean")
    out = out[INSERT_COLUMNS]

    buf = io.StringIO()
    out.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)

    cols = ", ".join(f'"{c}"' if c == "timestamp" else c for c in INSERT_COLUMNS)
    with conn.cursor() as cur:
        if fast:
            cur.execute("SET synchronous_commit = off;")
            cur.copy_expert(
                f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf
            )
            n = cur.rowcount
        else:
            cur.execute(
                f"CREATE TEMP TABLE _tf_stage "
                f"(LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP;"
            )
            cur.copy_expert(
                f"COPY _tf_stage ({cols}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf
            )
            cur.execute(
                f"INSERT INTO {table} ({cols}) "
                f"SELECT {cols} FROM _tf_stage "
                'ON CONFLICT (station_id, "timestamp", horizon_minutes) DO NOTHING;'
            )
            n = cur.rowcount
    conn.commit()
    return n


def build_month(conn, month_start: date, prox, trip, demand, cumflow,
                table=DEFAULT_TABLE, fast=False, legacy_capacity=None):
    src = weather_sources_for_month(month_start)
    if src is None:
        print(f"  {month_start:%Y-%m}  SKIP (gap / excluded year)", flush=True)
        return
    observed_tbl, forecast_tbl = src
    t0 = time.time()
    print(f"  {month_start:%Y-%m}  assembling...", flush=True)
    df = assemble(conn, month_start, observed_tbl, forecast_tbl, prox, trip, demand,
                  cumflow, legacy_capacity)
    if df.empty:
        print(f"  {month_start:%Y-%m}  no clean rows in window — nothing built", flush=True)
        return
    print(f"    copy... ({len(df):,} rows)", flush=True)
    n = copy_into_features(conn, df, table, fast=fast)
    by_h = df.groupby("horizon_minutes").size()
    detail = "  ".join(f"{h}m:{c:,}" for h, c in by_h.items())
    print(f"  {month_start:%Y-%m}  done {time.time()-t0:.0f}s  assembled={len(df):,}  inserted={n:,}  ({detail})", flush=True)


def _worker(args):
    """Multiprocessing.Pool worker — must be a top-level function (no lambdas) to pickle.

    Each worker gets its own DB connection and processes its slice of months sequentially.
    The static tables (prox, trip, demand, cumflow, legacy_capacity) are pickled once
    per worker at pool startup — a few MB total, not a bottleneck.
    """
    months, prox, trip, demand, cumflow, table, fast, legacy_capacity = args
    conn = get_conn()
    try:
        for month_start in months:
            build_month(conn, month_start, prox, trip, demand, cumflow, table, fast,
                        legacy_capacity)
    finally:
        conn.close()


def parse_month(s: str) -> date:
    return datetime.strptime(s, "%Y-%m").date().replace(day=1)


def main():
    ap = argparse.ArgumentParser(description="Build training_features (pandas, Option B).")
    ap.add_argument("--start", type=parse_month, help="first month, YYYY-MM")
    ap.add_argument("--end", type=parse_month, help="last month, YYYY-MM (inclusive)")
    ap.add_argument("--create-only", action="store_true", help="just run the DDL and exit")
    ap.add_argument("--table", default=DEFAULT_TABLE,
                    help="target table (default: training_features).")
    ap.add_argument("--fast", action="store_true",
                    help="skip staging table + turn off synchronous_commit. "
                         "Use for backfill after TRUNCATE — not safe for incremental runs.")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel worker processes (default 1). Set to CPU count for "
                         "backfill. Each worker gets its own DB connection and processes "
                         "an even slice of the month range.")
    args = ap.parse_args()

    conn = get_conn()
    try:
        create_table(conn, args.table)
        if args.create_only:
            return
        if not (args.start and args.end):
            ap.error("--start and --end are required unless --create-only")

        prox, trip, demand, cumflow = load_static_tables(conn)
        legacy_capacity = load_legacy_capacity(conn)
        month_list = list(months_between(args.start, args.end))

        if args.workers <= 1:
            for m in month_list:
                build_month(conn, m, prox, trip, demand, cumflow, args.table,
                            args.fast, legacy_capacity)
        else:
            n_workers = min(args.workers, len(month_list))
            # Split months into n_workers even chunks.
            chunks = [month_list[i::n_workers] for i in range(n_workers)]
            tasks = [(chunk, prox, trip, demand, cumflow, args.table, args.fast,
                      legacy_capacity)
                     for chunk in chunks]
            t0 = time.time()
            with multiprocessing.Pool(n_workers) as pool:
                pool.map(_worker, tasks)
            print(f"Done. {len(month_list)} months in {time.time()-t0:.0f}s "
                  f"({n_workers} workers).")
            return
    finally:
        conn.close()
    print("Done.")


if __name__ == "__main__":
    main()

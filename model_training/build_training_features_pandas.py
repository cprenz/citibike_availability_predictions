"""Phase 2 (feature stage, Option B) — build training_features in PANDAS.

This is the vectorized pandas rewrite of build_training_features.py. It reads the
same source (station_status_hourly_clean) and writes the same target table
(training_features) with the SAME columns, but replaces the slow correlated
LATERAL subqueries (one index probe per row, ~40 min / two months) with grouped
`.shift()` / `.rolling()` math on a complete hourly grid (~30-100x faster).

    station_status_hourly_clean   (clean, 1 row/station/hour)
            |  load month + back/fwd buffer, reindex to a COMPLETE hourly grid,
            |  vectorize lags/changes/rolling/targets, merge side tables   (pandas)
            v
    training_features

WHY A NEW FILE (not an edit of build_training_features.py): the SQL builder ran
successfully end-to-end on May+June 2026 and is kept as the validation reference
and fallback. Once this pandas builder is shown to reproduce its output
column-by-column, it becomes the default for the full backfill.

KEY SEMANTIC NOTES vs the SQL builder (mostly identical; two deliberate edges):
  - LAGS / TARGETS: the SQL builder used a fuzzy "most recent clean row at/before
    the lag time" (and "earliest at/after the target time, +2h tolerance"). Here we
    reindex every station to a COMPLETE hourly grid so every lag/target is an EXACT
    key via `.shift(k)` — a missing hour becomes NaN rather than silently borrowing
    an adjacent hour. On the (near-complete) clean table the two agree everywhere
    except at genuine hourly gaps, where this version is stricter (NaN). This is the
    documented Option-B end-state.
  - is_holiday is still FALSE here (matches the SQL builder's current stub). Wiring
    a real US-federal + NYC calendar is a separate next-build item.

The unpopulated-by-design columns (bikes_same_hour_same_weekday_4wk_avg,
emptying_frequency, capping_frequency, rebalancing_signal,
time_since_last_rebalancing, avg_availability_5_nearest_stations) are omitted from
the INSERT exactly as the SQL builder omits them, so they land as NULL in both.

Idempotent: COPY into a TEMP staging table then INSERT ... ON CONFLICT DO NOTHING
on the (station_id, timestamp, horizon_minutes) PK — same pattern as
build_clean_availability.py — so re-running a month is safe.

Usage (run from project root so `citibike` imports):
    python model_training/build_training_features_pandas.py --start 2026-05 --end 2026-06
    python model_training/build_training_features_pandas.py --start 2016-01 --end 2021-12
    python model_training/build_training_features_pandas.py --create-only   # just DDL

    # VALIDATION: write to a scratch table so this builder's rows sit beside the SQL
    # builder's `training_features` rows (no shared-PK collision) for a column diff:
    python model_training/build_training_features_pandas.py \
        --start 2026-05 --end 2026-06 --table training_features_pandas
"""

import argparse
import io
import multiprocessing
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import psycopg2
from dateutil.relativedelta import relativedelta

# Make `citibike` importable when run as a script from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citibike.config import DB_CONFIG, HORIZONS_MINUTES  # noqa: E402

DDL_PATH = Path(__file__).resolve().parents[1] / "sql" / "training_features.sql"

CLEAN_TBL = "station_status_hourly_clean"

# Horizons below this can't be built from hourly data (no sub-hourly snapshots).
MIN_HOURLY_HORIZON_MIN = 60

# The availability gap: no clean rows exist here, so months starting inside it are
# skipped (mirrors build_clean_availability.py / build_training_features.py).
GAP_START = date(2022, 1, 1)
GAP_END = date(2026, 5, 1)  # exclusive — May 2026 onward has live status data

# Load buffers around the target month (hours). Backward buffer feeds the longest
# lag (24h = same hour yesterday); forward buffer feeds the longest target horizon
# (2880min = 48h) so late-month rows still get a target from early next month —
# exactly what the SQL builder's unbounded LATERAL target lookup did.
BACK_BUFFER_HOURS = 24
FWD_BUFFER_HOURS = max(HORIZONS_MINUTES) // 60  # 2880min -> 48h

# Exact INSERT column order — IDENTICAL to build_training_features.py's INSERT, so
# the two builders' rows are directly comparable. Columns not listed here are left
# to their DB default (NULL): the stubbed lag/neighbor features.
INSERT_COLUMNS = [
    "station_id", "timestamp", "horizon_minutes",
    "bikes_available_at_horizon", "bike_available_binary",
    "hour_of_day", "day_of_week", "month", "season", "is_weekend", "is_holiday",
    "num_bikes_available", "num_ebikes_available", "num_docks_available", "num_bikes_disabled",
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
]

# Integer-typed feature columns (may contain NaN). Cast to pandas nullable Int64
# BEFORE COPY so to_csv writes "31"/"\N" and never "31.0" — the same float-promotion
# trap that bit the clean stage (an INTEGER column rejects "31.0").
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

# Nullable boolean columns -> pandas "boolean" dtype so NA serializes to \N.
BOOL_COLUMNS = ["is_weekend", "is_holiday", "is_within_400m"]

# Observed-weather feature columns (same names in the observed weather tables).
WEATHER_COLS = [
    "temperature_2m", "apparent_temperature", "precipitation", "rain",
    "snowfall", "wind_speed_10m", "cloud_cover", "relative_humidity_2m",
]


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


DEFAULT_TABLE = "training_features"


def create_table(conn, table: str = DEFAULT_TABLE):
    """Ensure the target table exists (idempotent).

    The canonical `training_features` is always created from the DDL file. A scratch
    target (e.g. `training_features_pandas`, used to validate this builder against
    the SQL builder's already-inserted rows WITHOUT colliding on the shared PK) is
    created as `LIKE training_features INCLUDING ALL` — same columns, PK, indexes —
    then turned into a hypertable. So the reference table always exists too.
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
    """Return (observed_tbl, forecast_tbl) for a month, or None if it's in the
    un-backfillable gap / excluded year. Availability always comes from CLEAN_TBL;
    only the weather tables differ by era."""
    if GAP_START <= month_start < GAP_END:
        return None
    if month_start.year == 2020:
        return None
    if month_start.year <= 2021:
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

def load_static_tables(conn):
    """Load the small, month-independent side tables once: subway proximity,
    per-station trip features, and the demand profile."""
    prox = pd.read_sql(
        "SELECT citibike_station_id AS station_id, nearest_entrance_dist_m, "
        "entrance_count_400m, entrance_count_800m, is_within_400m "
        "FROM citibike_station_subway_proximity;", conn)
    trip = pd.read_sql(
        "SELECT si.station_id, t.member_ratio, t.ebike_ratio, t.station_role "
        "FROM station_trip_features t "
        "JOIN station_information si ON si.short_name = t.station_id;", conn)
    demand = pd.read_sql(
        "SELECT si.station_id, d.hour_of_day, d.day_of_week, "
        "d.avg_departures AS avg_departures_this_hour_dow, "
        "d.avg_arrivals   AS avg_arrivals_this_hour_dow, "
        "d.avg_net_flow   AS avg_net_flow_this_hour_dow "
        "FROM station_demand_profile d "
        "JOIN station_information si ON si.short_name = d.station_id;", conn)
    return prox, trip, demand


def load_clean_window(conn, w_start, w_end) -> pd.DataFrame:
    """Pull clean hourly rows for [w_start, w_end) (month + buffers)."""
    sql = f"""
        SELECT station_id, hour, capacity,
               num_bikes_available, num_ebikes_available,
               num_docks_available, num_bikes_disabled
        FROM {CLEAN_TBL}
        WHERE hour >= %(w_start)s AND hour < %(w_end)s;
    """
    return pd.read_sql(sql, conn, params={"w_start": w_start, "w_end": w_end})


def load_observed_weather(conn, observed_tbl, w_start, w_end) -> pd.DataFrame:
    cols = ", ".join(WEATHER_COLS)
    sql = f"""
        SELECT "timestamp" AS hour, {cols}
        FROM {observed_tbl}
        WHERE "timestamp" >= %(w_start)s AND "timestamp" < %(w_end)s;
    """
    return pd.read_sql(sql, conn, params={"w_start": w_start, "w_end": w_end})


def load_flow(conn, w_start, w_end) -> pd.DataFrame:
    sql = """
        SELECT si.station_id, f.hour, f.departures, f.arrivals
        FROM station_hourly_flow f
        JOIN station_information si ON si.short_name = f.station_id
        WHERE f.hour >= %(w_start)s AND f.hour < %(w_end)s;
    """
    return pd.read_sql(sql, conn, params={"w_start": w_start, "w_end": w_end})


def load_forecast(conn, forecast_tbl, w_start, w_end) -> pd.DataFrame:
    """Forecast runs whose valid_time falls in the target window. We bound on
    valid_time (the hour being predicted) so every target's forecast is present."""
    cols = ", ".join(WEATHER_COLS)
    sql = f"""
        SELECT run_time, valid_time, lead_time_hours, {cols}
        FROM {forecast_tbl}
        WHERE valid_time >= %(w_start)s AND valid_time < %(w_end)s;
    """
    return pd.read_sql(sql, conn, params={"w_start": w_start, "w_end": w_end})


def forecast_for_horizon(fc: pd.DataFrame, lead_hours: int) -> pd.DataFrame:
    """Collapse the forecast table to ONE row per valid_time for this horizon.

    Reproduces the SQL builder's LATERAL: for valid_time = hour(ts+horizon), among
    runs issued at/before ts (run_time <= ts) pick the lead closest to `lead_hours`,
    tie-broken by most-recent run. Because ts = valid_time - horizon and the grid is
    hourly, "run_time <= ts" is exactly "lead_time_hours >= lead_hours"; the closest
    lead is then the SMALLEST such lead (= the most recent qualifying run). So:
    filter lead >= lead_hours, take min lead per valid_time."""
    c = fc[fc["lead_time_hours"] >= lead_hours]
    c = c.sort_values(["valid_time", "lead_time_hours"]).groupby("valid_time", as_index=False).first()
    rename = {col: f"forecast_{col}" for col in WEATHER_COLS}
    return c.rename(columns=rename)[["valid_time"] + list(rename.values())]


def build_grid(clean: pd.DataFrame, w_start, w_end) -> pd.DataFrame:
    """Reindex every station to a COMPLETE hourly grid over the load window, so
    gaps become explicit NaN and `.shift()` lags/targets are exact-key lookups."""
    full_index = pd.date_range(w_start, w_end, freq="h", inclusive="left", tz="UTC")
    stations = clean["station_id"].unique()
    mi = pd.MultiIndex.from_product([stations, full_index], names=["station_id", "hour"])
    grid = clean.set_index(["station_id", "hour"]).reindex(mi)
    # capacity is frozen per station; carry it onto filled rows so real month rows
    # divided in fill_ratio always have a denominator even if their own hour existed
    # only as a gap-fill (real rows already carry it; this is belt-and-suspenders).
    grid["capacity"] = grid.groupby(level=0)["capacity"].transform("max")
    return grid.sort_index()


def build_base_features(grid: pd.DataFrame) -> pd.DataFrame:
    """Compute all horizon-INDEPENDENT features on the hourly grid via grouped
    shifts/rolling. Returns a frame indexed (station_id, hour)."""
    g = grid.groupby(level=0)
    bikes = grid["num_bikes_available"]
    ebikes = grid["num_ebikes_available"]
    classic = bikes - ebikes
    cap = grid["capacity"]

    out = pd.DataFrame(index=grid.index)
    out["num_bikes_available"] = bikes
    out["num_ebikes_available"] = ebikes
    out["num_docks_available"] = grid["num_docks_available"]
    out["num_bikes_disabled"] = grid["num_bikes_disabled"]
    out["capacity"] = cap

    # lags (exact hourly keys; NaN at gaps)
    b1, b3, b6, b12 = g["num_bikes_available"].shift(1), g["num_bikes_available"].shift(3), \
        g["num_bikes_available"].shift(6), g["num_bikes_available"].shift(12)
    e1, e3, e6, e12 = g["num_ebikes_available"].shift(1), g["num_ebikes_available"].shift(3), \
        g["num_ebikes_available"].shift(6), g["num_ebikes_available"].shift(12)
    out["bikes_1hr_ago"], out["bikes_3hr_ago"] = b1, b3
    out["bikes_6hr_ago"], out["bikes_12hr_ago"] = b6, b12
    out["bikes_same_hour_yesterday"] = g["num_bikes_available"].shift(24)

    # capacity-normalized features. NULLIF(capacity, 0): cast to float first (so a
    # nullable-Int64 <NA> becomes np.nan cleanly), then mask 0 -> NaN. Dividing by
    # this yields NaN for zero/absent-capacity stations, matching the SQL builder.
    cap_safe = cap.astype("float64")
    cap_safe = cap_safe.mask(cap_safe == 0)
    out["fill_ratio"] = bikes / cap_safe
    out["fill_ratio_change_1hr"] = (bikes - b1) / cap_safe
    roll6 = g["num_bikes_available"].rolling(6, min_periods=1).mean().reset_index(level=0, drop=True)
    out["rolling_mean_fill_ratio_6hr"] = roll6 / cap_safe

    # count-change features (absolute deltas vs lag; classic = total - ebikes).
    # Lagged classic is derived from the lagged total/ebike pairs so the GBFS
    # total/ebike overlap never double-counts.
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

    # sub-hourly rates: not computable from hourly data (NULL, see 2026-06-15 decision)
    out["rate_of_change_10min"] = pd.NA
    out["rate_of_change_20min"] = pd.NA
    out["rate_of_change_30min"] = pd.NA
    return out


def add_targets(base: pd.DataFrame, grid: pd.DataFrame, horizons_hours):
    """Attach a target column per horizon: bikes at (hour + horizon), exact key."""
    g = grid.groupby(level=0)["num_bikes_available"]
    targets = {}
    for h in horizons_hours:
        targets[h] = g.shift(-h)  # value h hours ahead
    return targets


def assemble(conn, month_start: date, observed_tbl, forecast_tbl,
             prox, trip, demand) -> pd.DataFrame:
    """Build the long-form (one row per station x hour x horizon) feature frame for
    one month, ready to COPY."""
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
    print(f"    load_clean {elapsed()} ({len(clean):,} rows)", flush=True)

    grid = build_grid(clean, w_start, w_end)
    print(f"    build_grid {elapsed()}", flush=True)

    base = build_base_features(grid)
    print(f"    base_features {elapsed()}", flush=True)

    horizons = [h for h in HORIZONS_MINUTES if h >= MIN_HOURLY_HORIZON_MIN]
    horizons_hours = [h // 60 for h in horizons]
    targets = add_targets(base, grid, horizons_hours)

    # --- pick the rows to EMIT: target-month hours that were REAL observations.
    #     The grid was reindexed to a complete hourly spine so .shift() lags/targets
    #     are exact-key, but most gap-fill slots are NOT real station-hours. The SQL
    #     builder emits one row per actual row in station_status_hourly_clean, so we
    #     match that by requiring a non-null num_bikes_available (the clean stage
    #     guarantees 0 nulls, so notna() == "this hour was actually observed").
    #     Buffers (back 24h, forward 48h) only ever feed lags/targets, never emit. ---
    hour_idx = base.index.get_level_values("hour")
    emit = ((hour_idx >= m_start_ts) & (hour_idx < m_end_ts)
            & base["num_bikes_available"].notna().to_numpy())
    base = base[emit].reset_index()               # station_id, hour, features...
    targets = {h: t[emit].reset_index(drop=True) for h, t in targets.items()}
    print(f"    emit_filter {elapsed()} ({len(base):,} rows)", flush=True)

    # --- time features (Postgres EXTRACT(DOW): Sunday=0 .. Saturday=6) ---
    hr = base["hour"]
    base["hour_of_day"] = hr.dt.hour
    pg_dow = (hr.dt.dayofweek + 1) % 7      # pandas Mon=0 -> Postgres Sun=0
    base["day_of_week"] = pg_dow
    base["month"] = hr.dt.month
    base["season"] = season_of(base["month"])
    base["is_weekend"] = pg_dow.isin([0, 6])
    base["is_holiday"] = False              # stub (matches SQL builder); TODO: holidays pkg

    # --- observed weather, static, demand (same for every horizon) ---
    obs = load_observed_weather(conn, observed_tbl, w_start, w_end)
    base = base.merge(obs, on="hour", how="left")
    print(f"    merge_weather {elapsed()}", flush=True)
    base = base.merge(prox, on="station_id", how="left")
    base = base.merge(trip, on="station_id", how="left")

    flow = load_flow(conn, m_start_ts, m_end_ts)
    print(f"    load_flow {elapsed()} ({len(flow):,} rows)", flush=True)
    base = base.merge(flow, on=["station_id", "hour"], how="left")
    # COALESCE(..., 0): stations with no trips this hour get 0 departures/arrivals.
    # to_numeric avoids the object-dtype fillna downcast warning on the left-join NaNs.
    base["departures_this_hour"] = pd.to_numeric(base["departures"], errors="coerce").fillna(0)
    base["arrivals_this_hour"] = pd.to_numeric(base["arrivals"], errors="coerce").fillna(0)
    base = base.merge(
        demand, left_on=["station_id", "hour_of_day", "day_of_week"],
        right_on=["station_id", "hour_of_day", "day_of_week"], how="left")
    print(f"    merge_demand {elapsed()}", flush=True)

    # --- explode to long form: one block per horizon with target + forecast wx ---
    fc_raw = load_forecast(conn, forecast_tbl, w_start, w_end)
    print(f"    load_forecast {elapsed()}", flush=True)
    blocks = []
    for h_min, h_hr in zip(horizons, horizons_hours):
        blk = base.copy()
        blk["horizon_minutes"] = h_min
        tgt = targets[h_hr]
        blk["bikes_available_at_horizon"] = tgt.values
        blk = blk[blk["bikes_available_at_horizon"].notna()].copy()   # drop NULL targets
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

    fast=False (default): COPY → temp staging → INSERT ON CONFLICT DO NOTHING.
        Safe for incremental monthly retrain runs where rows may already exist.
    fast=True: COPY directly, synchronous_commit=off, no conflict check.
        Use for backfill after a TRUNCATE — ~2-3x faster per month.
        If the process dies mid-run: TRUNCATE and re-run.
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


def build_month(conn, month_start: date, prox, trip, demand,
                table=DEFAULT_TABLE, fast=False):
    src = weather_sources_for_month(month_start)
    if src is None:
        print(f"  {month_start:%Y-%m}  SKIP (gap / excluded year)", flush=True)
        return
    observed_tbl, forecast_tbl = src
    t0 = time.time()
    print(f"  {month_start:%Y-%m}  assembling...", flush=True)
    df = assemble(conn, month_start, observed_tbl, forecast_tbl, prox, trip, demand)
    if df.empty:
        print(f"  {month_start:%Y-%m}  no clean rows in window — nothing built", flush=True)
        return
    print(f"    copy... ({len(df):,} rows)", flush=True)
    n = copy_into_features(conn, df, table, fast=fast)
    by_h = df.groupby("horizon_minutes").size()
    detail = "  ".join(f"{h}m:{c:,}" for h, c in by_h.items())
    print(f"  {month_start:%Y-%m}  done {time.time()-t0:.0f}s  assembled={len(df):,}  inserted={n:,}  ({detail})", flush=True)


def _worker(args):
    """Top-level worker for multiprocessing.Pool — must be picklable (no lambdas).
    Each worker opens its own DB connection and processes its assigned months
    sequentially. prox/trip/demand DataFrames are passed by value (pickled once
    per worker at pool startup, ~few MB total)."""
    months, prox, trip, demand, table, fast = args
    conn = get_conn()
    try:
        for month_start in months:
            build_month(conn, month_start, prox, trip, demand, table, fast)
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

        prox, trip, demand = load_static_tables(conn)
        month_list = list(months_between(args.start, args.end))

        if args.workers <= 1:
            for m in month_list:
                build_month(conn, m, prox, trip, demand, args.table, args.fast)
        else:
            n_workers = min(args.workers, len(month_list))
            # Split months into n_workers even chunks.
            chunks = [month_list[i::n_workers] for i in range(n_workers)]
            tasks = [(chunk, prox, trip, demand, args.table, args.fast)
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

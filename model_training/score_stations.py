"""Hourly scoring script — writes predictions for all active stations to model_predictions.

Run from project root:
    python model_training/score_stations.py

Schedule via Windows Task Scheduler to run every hour. Each run builds one feature row
per active station x 6 horizons (~14,400 rows), scores all 18 models, and upserts
results to model_predictions.

Feature construction mirrors build_training_features_pandas.py:
  - Station status lags come from station_status (live GBFS), point-sampled to hourly
  - Static features (proximity, trip, demand, cumflow) loaded from their tables
  - Observed weather from weather_post2021_openmeteo_observed
  - Forecast weather from weather_post2021_openmeteo_forecast, matched per horizon
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citibike.config import DB_CONFIG, MODELS_DIR
from model_training.feature_prep import FEATURE_COLS
from model_training.build_training_features_pandas import (
    WEATHER_COLS,
    _normalize_legacy_ids,
    forecast_for_horizon,
    load_static_tables,
    season_of,
)

HORIZONS = [60, 180, 360, 720, 1440, 2880]
HORIZON_LABELS = {60: "1hr", 180: "3hr", 360: "6hr",
                  720: "12hr", 1440: "24hr", 2880: "multi-day"}

# Cap on how many missed hourly runs get auto-replayed on startup. A multi-day
# outage likely means station_status has a matching gap anyway (ingest.py was
# down too), so replaying past this point wouldn't recover real predictions —
# just cuts off the catch-up loop rather than replaying an unbounded backlog.
MAX_BACKFILL_HOURS = 48

_BOOL_SCORE_COLS = ["is_weekend", "is_holiday", "is_within_400m",
                    "num_ebikes_was_null", "num_bikes_disabled_was_null"]
_SUBWAY_SENTINEL_COL = "nearest_entrance_dist_m"
_SUBWAY_SENTINEL_VAL = 800.0


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_conn():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
    return conn


# ---------------------------------------------------------------------------
# Model artifacts
# ---------------------------------------------------------------------------

def load_artifacts():
    """Load all 18 .joblib artifacts from models/."""
    lgbm, linear, logistic = {}, {}, {}
    for h in HORIZONS:
        lgbm[h]     = joblib.load(MODELS_DIR / f"lgbm_regression_{h}min.joblib")
        linear[h]   = joblib.load(MODELS_DIR / f"linear_regression_{h}min.joblib")
        logistic[h] = joblib.load(MODELS_DIR / f"logistic_classification_{h}min.joblib")
    print(f"Loaded 18 artifacts ({len(HORIZONS)} horizons x 3 models)")
    return lgbm, linear, logistic


# ---------------------------------------------------------------------------
# Status: pull from station_status and point-sample to hourly
# ---------------------------------------------------------------------------

def load_recent_status(conn, now_hour: pd.Timestamp) -> pd.DataFrame:
    """25 hours of station_status, point-sampled to one row per (station, hour).

    25 hours covers: current (0) plus lags 1/3/6/12/24hr and the 6hr rolling window.
    Point-sample = last snapshot per station-hour, same logic as build_clean_availability.py.
    """
    since = now_hour - pd.Timedelta(hours=25)
    df = pd.read_sql("""
        SELECT station_id, fetched_at,
               num_bikes_available, num_ebikes_available,
               num_docks_available, num_bikes_disabled
        FROM station_status
        WHERE fetched_at >= %(since)s
          AND num_bikes_available >= 0
        ORDER BY fetched_at;
    """, conn, params={"since": since})
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    df["hour"]       = df["fetched_at"].dt.floor("h")
    df = (df.sort_values("fetched_at")
            .groupby(["station_id", "hour"], as_index=False)
            .last()
            .drop(columns=["fetched_at"]))
    return df


# ---------------------------------------------------------------------------
# Feature construction
# ---------------------------------------------------------------------------

def build_feature_base(status: pd.DataFrame, conn, now_hour: pd.Timestamp) -> pd.DataFrame:
    """Build the horizon-independent feature row for each active station at now_hour.

    Mirrors build_base_features() from the monthly builder but operates on a 25-hour
    window from station_status rather than a full monthly clean window.
    """
    hours = pd.date_range(now_hour - pd.Timedelta(hours=24), now_hour, freq="h", tz="UTC")
    stations = status["station_id"].unique()
    mi   = pd.MultiIndex.from_product([stations, hours], names=["station_id", "hour"])
    grid = status.set_index(["station_id", "hour"]).reindex(mi).sort_index()

    bikes   = grid["num_bikes_available"]
    ebikes  = grid["num_ebikes_available"].fillna(0)
    classic = bikes - ebikes
    g       = grid.groupby(level=0)

    # Capacity from station_information — broadcast per station across all hours.
    cap_df  = pd.read_sql(
        "SELECT station_id, capacity FROM station_information WHERE capacity > 0;", conn)
    cap_map = cap_df.set_index("station_id")["capacity"].astype("float64")
    cap_raw = pd.Series(
        grid.index.get_level_values("station_id").map(cap_map).astype("float64"),
        index=grid.index)
    cap_s = cap_raw.mask(cap_raw == 0)

    b1  = g["num_bikes_available"].shift(1)
    b3  = g["num_bikes_available"].shift(3)
    b6  = g["num_bikes_available"].shift(6)
    b12 = g["num_bikes_available"].shift(12)
    b24 = g["num_bikes_available"].shift(24)
    e1  = g["num_ebikes_available"].shift(1).fillna(0)
    e3  = g["num_ebikes_available"].shift(3).fillna(0)
    e6  = g["num_ebikes_available"].shift(6).fillna(0)
    e12 = g["num_ebikes_available"].shift(12).fillna(0)
    cl1  = b1  - e1;  cl3  = b3  - e3
    cl6  = b6  - e6;  cl12 = b12 - e12

    roll6 = (g["num_bikes_available"].rolling(6, min_periods=1)
               .mean().reset_index(level=0, drop=True))

    feat = pd.DataFrame(index=grid.index)
    feat["num_bikes_available"]         = bikes
    feat["num_ebikes_was_null"]         = grid["num_ebikes_available"].isna()
    feat["num_ebikes_available"]        = ebikes
    feat["num_docks_available"]         = grid["num_docks_available"]
    feat["num_bikes_disabled_was_null"] = grid["num_bikes_disabled"].isna()
    feat["num_bikes_disabled"]          = grid["num_bikes_disabled"].fillna(0)
    feat["capacity"]                    = cap_s

    feat["bikes_1hr_ago"]             = b1
    feat["bikes_3hr_ago"]             = b3
    feat["bikes_6hr_ago"]             = b6
    feat["bikes_12hr_ago"]            = b12
    feat["bikes_same_hour_yesterday"] = b24

    feat["fill_ratio"]                = bikes / cap_s
    feat["fill_ratio_change_1hr"]     = (bikes - b1) / cap_s
    feat["rolling_mean_fill_ratio_6hr"] = roll6 / cap_s

    feat["change_bikes_1hr"],   feat["change_bikes_3hr"]    = bikes - b1,   bikes - b3
    feat["change_bikes_6hr"],   feat["change_bikes_12hr"]   = bikes - b6,   bikes - b12
    feat["change_ebikes_1hr"],  feat["change_ebikes_3hr"]   = ebikes - e1,  ebikes - e3
    feat["change_ebikes_6hr"],  feat["change_ebikes_12hr"]  = ebikes - e6,  ebikes - e12
    feat["change_classic_1hr"], feat["change_classic_3hr"]  = classic - cl1, classic - cl3
    feat["change_classic_6hr"], feat["change_classic_12hr"] = classic - cl6, classic - cl12

    # Trim to now_hour; drop stations with no current reading.
    out = feat.xs(now_hour, level="hour")
    out = out[out["num_bikes_available"].notna()].copy()
    out.index.name = "station_id"
    return out.reset_index()


def add_time_features(df: pd.DataFrame, now_hour: pd.Timestamp) -> pd.DataFrame:
    df = df.copy()
    df["hour_of_day"] = now_hour.hour
    df["day_of_week"] = now_hour.dayofweek
    df["month"]       = now_hour.month
    df["season"]      = season_of(pd.Series([now_hour.month] * len(df))).values
    df["is_weekend"]  = float(now_hour.dayofweek >= 5)
    df["is_holiday"]  = 0.0   # stub — matches the training stub
    df["hour_sin"]    = np.sin(2 * np.pi * now_hour.hour   / 24)
    df["hour_cos"]    = np.cos(2 * np.pi * now_hour.hour   / 24)
    df["dow_sin"]     = np.sin(2 * np.pi * now_hour.dayofweek / 7)
    df["dow_cos"]     = np.cos(2 * np.pi * now_hour.dayofweek / 7)
    df["month_sin"]   = np.sin(2 * np.pi * now_hour.month  / 12)
    df["month_cos"]   = np.cos(2 * np.pi * now_hour.month  / 12)
    return df


def add_static_features(df: pd.DataFrame, prox, trip, demand, cumflow,
                        now_hour: pd.Timestamp) -> pd.DataFrame:
    """Merge subway proximity, trip summary, demand profile, and cumulative flow."""
    df = df.merge(prox, on="station_id", how="left")
    df = df.merge(trip, on="station_id", how="left")

    demand_now = demand[
        (demand["hour_of_day"] == now_hour.hour) &
        (demand["day_of_week"] == now_hour.dayofweek)
    ].drop(columns=["hour_of_day", "day_of_week"])
    df = df.merge(demand_now, on="station_id", how="left")

    cumflow_now = cumflow[
        (cumflow["hour_of_day"] == now_hour.hour) &
        (cumflow["day_of_week"] == now_hour.dayofweek)
    ].drop(columns=["hour_of_day", "day_of_week"])
    df = df.merge(cumflow_now, on="station_id", how="left")

    # Structural NULL = no subway entrance within 800m; fill with the cutoff sentinel.
    df[_SUBWAY_SENTINEL_COL] = df[_SUBWAY_SENTINEL_COL].fillna(_SUBWAY_SENTINEL_VAL)

    # Cast booleans to float — matches load_training_data() in feature_prep.py.
    for col in _BOOL_SCORE_COLS:
        if col in df.columns:
            df[col] = df[col].fillna(False).astype(float)

    return df


def add_flow_features(df: pd.DataFrame, conn, now_hour: pd.Timestamp) -> pd.DataFrame:
    """Merge current-hour trip counts and net-flow lags from station_hourly_flow.

    station_hourly_flow.hour stores NYC local time as if it were UTC (the same timezone
    quirk fixed in notebooks 1.01 and 1.03). Convert each UTC lag time to its NYC local
    equivalent before querying.
    """
    def flow_key(utc_ts: pd.Timestamp) -> pd.Timestamp:
        local = utc_ts.tz_convert("America/New_York")
        return pd.Timestamp(local.year, local.month, local.day, local.hour, tz="UTC")

    lag_hours = [0, 1, 3, 6]
    keys = {h: flow_key(now_hour - pd.Timedelta(hours=h)) for h in lag_hours}
    since, until = min(keys.values()), max(keys.values())

    flow_m = pd.read_sql("""
        SELECT si.station_id, f.hour, f.departures, f.arrivals
        FROM station_hourly_flow f
        JOIN station_information si ON si.short_name = f.station_id
        WHERE f.hour >= %(since)s AND f.hour <= %(until)s;
    """, conn, params={"since": since, "until": until})
    flow_l = pd.read_sql("""
        SELECT station_id, hour, departures, arrivals
        FROM station_hourly_flow
        WHERE hour >= %(since)s AND hour <= %(until)s;
    """, conn, params={"since": since, "until": until})
    flow_l["station_id"] = _normalize_legacy_ids(flow_l["station_id"])
    flow = (pd.concat([flow_m, flow_l], ignore_index=True)
              .drop_duplicates(["station_id", "hour"]))
    flow["hour"] = pd.to_datetime(flow["hour"], utc=True)
    flow["net"]  = flow["arrivals"] - flow["departures"]

    def pivot_at(key, **col_map):
        sub = flow[flow["hour"] == key].set_index("station_id")
        return pd.DataFrame({new: sub[src] for new, src in col_map.items()})

    flow_feats = pd.concat([
        pivot_at(keys[0], departures_this_hour="departures", arrivals_this_hour="arrivals"),
        pivot_at(keys[1], net_flow_1hr="net"),
        pivot_at(keys[3], net_flow_3hr="net"),
        pivot_at(keys[6], net_flow_6hr="net"),
    ], axis=1).reset_index().rename(columns={"index": "station_id"})

    return df.merge(flow_feats, on="station_id", how="left")


def add_observed_weather(df: pd.DataFrame, conn, now_hour: pd.Timestamp) -> pd.DataFrame:
    """Broadcast the most recent observed weather row to all station rows.

    Queries weather_realtime first (near-realtime, <1hr lag, populated hourly by
    ingest_weather_realtime.py). Falls back to weather_post2021_openmeteo_observed
    (ERA5, ~5 day lag) only if the realtime table has no recent row. This eliminates
    the train/serve mismatch where scoring previously used 5-day-old weather as
    current conditions.
    """
    obs = pd.read_sql("""
        SELECT temperature_2m, apparent_temperature, precipitation, rain,
               snowfall, wind_speed_10m, cloud_cover, relative_humidity_2m
        FROM weather_realtime
        WHERE timestamp <= %(t)s
        ORDER BY timestamp DESC LIMIT 1;
    """, conn, params={"t": now_hour})

    if obs.empty:
        print("  weather_realtime empty — falling back to ERA5 observed.")
        obs = pd.read_sql("""
            SELECT temperature_2m, apparent_temperature, precipitation, rain,
                   snowfall, wind_speed_10m, cloud_cover, relative_humidity_2m
            FROM weather_post2021_openmeteo_observed
            WHERE timestamp <= %(t)s
            ORDER BY timestamp DESC LIMIT 1;
        """, conn, params={"t": now_hour})

    for col in WEATHER_COLS:
        df[col] = obs[col].iloc[0] if not obs.empty and col in obs.columns else np.nan
    return df


def load_forecast(conn, now_hour: pd.Timestamp) -> pd.DataFrame:
    """Forecast runs whose valid_time covers now_hour → now_hour + max horizon."""
    w_end = now_hour + pd.Timedelta(minutes=max(HORIZONS) + 60)
    fc = pd.read_sql("""
        SELECT run_time, valid_time, lead_time_hours,
               temperature_2m, apparent_temperature, precipitation, rain,
               snowfall, wind_speed_10m, cloud_cover, relative_humidity_2m
        FROM weather_post2021_openmeteo_forecast
        WHERE valid_time > %(now)s AND valid_time <= %(end)s;
    """, conn, params={"now": now_hour, "end": w_end})
    fc["valid_time"] = pd.to_datetime(fc["valid_time"], utc=True)
    fc["run_time"]   = pd.to_datetime(fc["run_time"],   utc=True)
    return fc


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_horizon(base: pd.DataFrame, h: int, fc: pd.DataFrame,
                  now_hour: pd.Timestamp,
                  lgbm_model, linear_artifact, logistic_model) -> tuple:
    """Add forecast weather for this horizon, score all 3 models.

    Returns (predictions_df, stats_dict) where stats_dict contains per-model
    prediction counts for scoring_log.
    """
    target_time = now_hour + pd.Timedelta(minutes=h)
    fc_h  = forecast_for_horizon(fc, h // 60)
    fc_row = fc_h[fc_h["valid_time"] == target_time]

    row = base.copy()
    for col in WEATHER_COLS:
        fc_col = f"forecast_{col}"
        row[fc_col] = (fc_row[fc_col].values[0]
                       if not fc_row.empty and fc_col in fc_row.columns
                       else np.nan)

    X = row[FEATURE_COLS].copy()

    lgbm_pred   = lgbm_model.predict(X)
    linear_pred = linear_artifact["pipeline"].predict(X)
    logit_prob  = logistic_model.predict_proba(X)[:, 1]

    # Same serving pattern as 2.04b/2.08b/2.08c: point prediction + stored offsets,
    # lower bound clipped at 0 (can't have negative bikes).
    pi_lower = np.clip(linear_pred + linear_artifact["pi_lower_offset"], 0, None)
    pi_upper = linear_pred + linear_artifact["pi_upper_offset"]

    preds = pd.DataFrame({
        "station_id":              row["station_id"].values,
        "horizon_minutes":         h,
        "predicted_value_lgbm":    lgbm_pred,
        "predicted_value_linear":  linear_pred,
        "pi_lower":                pi_lower,
        "pi_upper":                pi_upper,
        "predicted_prob_logistic": logit_prob,
    })

    stats = {
        "lgbm_predictions":     int(np.sum(~np.isnan(lgbm_pred))),
        "linear_predictions":   int(np.sum(~np.isnan(linear_pred))),
        "logistic_predictions": int(np.sum(~np.isnan(logit_prob))),
    }
    return preds, stats


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_scoring_log(conn, scored_at: pd.Timestamp, horizon_stats: list, status: str,
                      error_message: str = None):
    """Write one row per model per horizon to scoring_log (18 rows per run)."""
    rows = []
    for s in horizon_stats:
        for model_type, count in [
            ("lgbm",     s["lgbm_predictions"]),
            ("linear",   s["linear_predictions"]),
            ("logistic", s["logistic_predictions"]),
        ]:
            rows.append((scored_at, s["horizon_minutes"], model_type,
                         count, s["duration_seconds"], status, error_message))
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO scoring_log
                (scored_at, horizon_minutes, model_type, predictions_written,
                 duration_seconds, status, error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, rows)
    conn.commit()


def _to_float_or_none(v):
    return float(v) if pd.notna(v) else None


def write_predictions(conn, rows: pd.DataFrame, predicted_at: pd.Timestamp):
    """Upsert all predictions to model_predictions in a single transaction."""
    records = [
        (r["station_id"], predicted_at, int(r["horizon_minutes"]),
         predicted_at + pd.Timedelta(minutes=int(r["horizon_minutes"])),
         _to_float_or_none(r["predicted_value_lgbm"]),
         _to_float_or_none(r["predicted_value_linear"]),
         _to_float_or_none(r["pi_lower"]),
         _to_float_or_none(r["pi_upper"]),
         _to_float_or_none(r["predicted_prob_logistic"]))
        for _, r in rows.iterrows()
    ]
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO model_predictions
                (station_id, predicted_at, horizon_minutes, target_time,
                 predicted_value_lgbm, predicted_value_linear,
                 pi_lower, pi_upper, predicted_prob_logistic)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (station_id, predicted_at, horizon_minutes) DO UPDATE
                SET target_time              = EXCLUDED.target_time,
                    predicted_value_lgbm      = EXCLUDED.predicted_value_lgbm,
                    predicted_value_linear    = EXCLUDED.predicted_value_linear,
                    pi_lower                  = EXCLUDED.pi_lower,
                    pi_upper                  = EXCLUDED.pi_upper,
                    predicted_prob_logistic   = EXCLUDED.predicted_prob_logistic;
        """, records)
    conn.commit()
    print(f"Wrote {len(records):,} rows to model_predictions")


# ---------------------------------------------------------------------------
# Actual value backfill
# ---------------------------------------------------------------------------

def backfill_actuals(conn, now_hour: pd.Timestamp):
    """Fill actual_value for past predictions whose horizon has now passed.

    For each NULL actual_value row where predicted_at + horizon_minutes <= now_hour,
    looks up the first station_status reading at or after the target time.
    station_status polls every ~2.5 min so there is always a reading close to the hour.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE model_predictions mp
            SET actual_value = (
                SELECT num_bikes_available
                FROM station_status ss
                WHERE ss.station_id = mp.station_id
                  AND ss.fetched_at >= mp.predicted_at
                                     + (mp.horizon_minutes || ' minutes')::INTERVAL
                ORDER BY ss.fetched_at ASC
                LIMIT 1
            )
            WHERE mp.actual_value IS NULL
              AND mp.predicted_at
                  + (mp.horizon_minutes || ' minutes')::INTERVAL <= %(now)s;
        """, {"now": now_hour})
        updated = cur.rowcount
    conn.commit()
    print(f"Backfilled {updated} actual values")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_scoring_pass(conn, artifacts: tuple, now_hour: pd.Timestamp):
    """Score all active stations across all horizons for one target hour.

    now_hour can be the current hour (live run) or a past hour (auto-backfill
    replay) — every helper this calls takes now_hour as a parameter rather than
    reading the clock, so replaying a past hour reconstructs the feature state
    as it existed then, from whatever station_status/weather data still covers it.
    """
    lgbm_models, linear_models, logistic_models = artifacts
    t0 = time.time()
    print(f"Scoring at {now_hour.isoformat()}")

    print("Loading static tables...")
    prox, trip, demand, cumflow = load_static_tables(conn)

    print("Loading recent station_status...")
    status = load_recent_status(conn, now_hour)
    print(f"  {status['station_id'].nunique()} stations, {len(status)} hourly snapshots")

    print("Building base features...")
    base = build_feature_base(status, conn, now_hour)
    print(f"  {len(base)} active stations")

    if base.empty:
        msg = "no active stations at this hour (station_status gap — unrecoverable)"
        print(f"  {msg}")
        empty_stats = [
            {"horizon_minutes": h, "duration_seconds": 0.0, "lgbm_predictions": 0,
             "linear_predictions": 0, "logistic_predictions": 0}
            for h in HORIZONS
        ]
        write_scoring_log(conn, now_hour, empty_stats, status="skipped", error_message=msg)
        return

    base = add_time_features(base, now_hour)
    base = add_static_features(base, prox, trip, demand, cumflow, now_hour)
    base = add_flow_features(base, conn, now_hour)
    base = add_observed_weather(base, conn, now_hour)

    print("Loading forecast weather...")
    fc = load_forecast(conn, now_hour)
    print(f"  {len(fc)} forecast rows")

    print("Scoring horizons...")
    all_preds = []
    horizon_stats = []
    for h in HORIZONS:
        h_start = time.time()
        preds, stats = score_horizon(
            base, h, fc, now_hour,
            lgbm_models[h], linear_models[h], logistic_models[h])
        stats["horizon_minutes"] = h
        stats["duration_seconds"] = round(time.time() - h_start, 2)
        all_preds.append(preds)
        horizon_stats.append(stats)
        print(f"  {HORIZON_LABELS[h]:10s} -> "
              f"lgbm={stats['lgbm_predictions']}  "
              f"linear={stats['linear_predictions']}  "
              f"logistic={stats['logistic_predictions']}")

    all_preds = pd.concat(all_preds, ignore_index=True)
    write_predictions(conn, all_preds, now_hour)
    write_scoring_log(conn, now_hour, horizon_stats, status="success")
    print(f"Done in {time.time() - t0:.1f}s")


def get_missed_hours(conn, now_hour: pd.Timestamp) -> list:
    """Hours strictly between the last successful scoring run and now_hour.

    Reads scoring_log directly rather than requiring a flag — every run checks
    for and replays any gap since the last success automatically. Capped at
    MAX_BACKFILL_HOURS (see constant above). Returns [] on the very first run
    (no prior success to measure a gap from).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(scored_at) FROM scoring_log WHERE status = 'success';")
        last_success = cur.fetchone()[0]

    if last_success is None:
        return []

    last_success = pd.Timestamp(last_success).tz_convert("UTC")
    gap_hours = int((now_hour - last_success) / pd.Timedelta(hours=1)) - 1
    gap_hours = max(0, min(gap_hours, MAX_BACKFILL_HOURS))
    return [last_success + pd.Timedelta(hours=i) for i in range(1, gap_hours + 1)]


def main():
    t0 = time.time()
    now_utc  = datetime.now(timezone.utc)
    now_hour = pd.Timestamp(now_utc).floor("h").tz_convert("UTC")

    print("Loading artifacts...")
    artifacts = load_artifacts()

    conn = get_conn()
    try:
        print("Backfilling actuals...")
        backfill_actuals(conn, now_hour)

        missed = get_missed_hours(conn, now_hour)
        if missed:
            print(f"Auto-backfill: {len(missed)} missed hour(s) since last successful "
                  f"run ({missed[0].isoformat()} to {missed[-1].isoformat()})")
            for h in missed:
                run_scoring_pass(conn, artifacts, h)

        run_scoring_pass(conn, artifacts, now_hour)
        print(f"Total run time: {time.time() - t0:.1f}s")

    finally:
        conn.close()


if __name__ == "__main__":
    main()

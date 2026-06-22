"""Patch training_features with 14 Tier-1 columns (all additive — no existing column changed).

Three feature groups:

  1. Cyclical time encodings (6 cols)
     hour_sin/cos (period 24), dow_sin/cos (period 7), month_sin/cos (period 12).
     Pure math on existing hour_of_day / day_of_week / month columns.

  2. Cumulative expected net flow (5 cols)
     cumulative_expected_net_flow_{1,3,6,12,24}hr — sum of avg_net_flow_this_hour_dow
     across the next H hours from station_demand_profile. Precomputed once as a
     (station_id, hour_of_day, day_of_week) lookup; merged per month.

  3. Recent net-flow momentum lags (3 cols)
     net_flow_{1,3,6}hr = arrivals - departures from station_hourly_flow lagged
     1/3/6 hours. NULL wherever station_hourly_flow has no data (pre-2019, JC
     stations). Handle as missing-at-random in train_model.py (same as other lags).

Each month: reads unique (station_id, timestamp) from training_features (hypertable
partition prune keeps it fast), computes all 14 values in pandas, writes to a TEMP
staging table, then issues one UPDATE that hits ALL 6 horizon rows per timestamp
in a single pass.

Usage (run from project root):
    # Add the 14 columns to the live table first (idempotent):
    python model_training/patch_tier1_features.py --alter-only

    # Then patch all training years:
    python model_training/patch_tier1_features.py --start 2019-01 --end 2021-12 --workers 16
    python model_training/patch_tier1_features.py --start 2026-05 --end 2026-06 --workers 2

    # To patch a single month (useful for testing):
    python model_training/patch_tier1_features.py --start 2026-05 --end 2026-05
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
from citibike.config import DB_CONFIG  # noqa: E402

GAP_START = date(2022, 1, 1)
GAP_END   = date(2026, 5, 1)  # exclusive — matches builder

CUMFLOW_HORIZONS_H = [1, 3, 6, 12, 24]
FLOW_LAG_HOURS     = [1, 3, 6]

NEW_COLUMNS = [
    "hour_sin", "hour_cos",
    "dow_sin",  "dow_cos",
    "month_sin", "month_cos",
    "cumulative_expected_net_flow_1hr",
    "cumulative_expected_net_flow_3hr",
    "cumulative_expected_net_flow_6hr",
    "cumulative_expected_net_flow_12hr",
    "cumulative_expected_net_flow_24hr",
    "net_flow_1hr", "net_flow_3hr", "net_flow_6hr",
]

# Columns sent through COPY to the staging table
STAGE_COLS = ["station_id", "timestamp"] + NEW_COLUMNS


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def months_between(start: date, end: date):
    cur = start.replace(day=1)
    last = end.replace(day=1)
    while cur <= last:
        yield cur
        cur += relativedelta(months=1)


def parse_month(s: str) -> date:
    return datetime.strptime(s, "%Y-%m").date().replace(day=1)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def alter_table(conn):
    """Add the 14 new columns idempotently (safe to re-run)."""
    cur = conn.cursor()
    for col in NEW_COLUMNS:
        cur.execute(
            f"ALTER TABLE training_features "
            f"ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION;"
        )
    conn.commit()
    print(f"ALTER TABLE done — {len(NEW_COLUMNS)} columns added (or already present).")


# ---------------------------------------------------------------------------
# Precomputed cumulative net-flow lookup
# ---------------------------------------------------------------------------

def build_cumflow_lookup(conn) -> pd.DataFrame:
    """Return a DataFrame indexed by (station_id, hour_of_day, day_of_week)
    with one column per cumulative-net-flow horizon.

    Loads station_demand_profile in BOTH namespaces (same pattern as the builder):
      modern  — JOIN station_information.short_name → UUID station_id
                (matches post-2021 training_features rows)
      legacy  — raw station_id normalized '116.0' → '116'
                (matches pre-2021 training_features rows)
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

    # Pivot to (station_id × 168 week-hours) matrix; fill missing slots with 0.
    piv = (demand.pivot_table(index="station_id", columns="week_hour",
                               values="avg_net_flow", aggfunc="first",
                               fill_value=0.0)
                 .reindex(columns=range(168), fill_value=0.0))

    mat  = piv.values                    # (n_stations, 168)
    mat2 = np.hstack([mat, mat])         # (n_stations, 336) — handles wrap-around
    cs   = np.cumsum(mat2, axis=1)       # prefix sums for O(1) range queries

    station_ids = piv.index.values
    n = len(station_ids)

    blocks = []
    for h in range(168):
        rec: dict = {
            "station_id":  station_ids,
            "hour_of_day": np.full(n, h % 24, dtype=np.int16),
            "day_of_week": np.full(n, h // 24, dtype=np.int16),
        }
        for H in CUMFLOW_HORIZONS_H:
            # sum of the next H hours: cs[:, h+H] - cs[:, h]
            rec[f"cumulative_expected_net_flow_{H}hr"] = cs[:, h + H] - cs[:, h]
        blocks.append(pd.DataFrame(rec))

    lookup = pd.concat(blocks, ignore_index=True)
    print(f"  cumflow lookup built: {len(lookup):,} rows "
          f"({n} stations × 168 week-hours)", flush=True)
    return lookup


# ---------------------------------------------------------------------------
# Per-month data helpers
# ---------------------------------------------------------------------------

def load_month_timestamps(conn, m_start, m_end) -> pd.DataFrame:
    """Unique (station_id, timestamp) for the month, plus time-feature columns
    needed for cyclic encoding and the cumflow merge."""
    df = pd.read_sql(
        """SELECT DISTINCT station_id, "timestamp", hour_of_day, day_of_week, month
           FROM training_features
           WHERE "timestamp" >= %(start)s AND "timestamp" < %(end)s;""",
        conn, params={"start": m_start, "end": m_end})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def load_flow_dual(conn, w_start, w_end) -> pd.DataFrame:
    """Load station_hourly_flow in both namespaces (modern UUID + legacy integer),
    deduplicate, and return (station_id, hour, net_flow).

    Mirrors build_training_features_pandas.load_flow() so the station_id values
    match what is already stored in training_features."""
    flow_modern = pd.read_sql(
        """SELECT si.station_id, f.hour, f.arrivals, f.departures
           FROM station_hourly_flow f
           JOIN station_information si ON si.short_name = f.station_id
           WHERE f.hour >= %(start)s AND f.hour < %(end)s;""",
        conn, params={"start": w_start, "end": w_end})
    flow_legacy = pd.read_sql(
        """SELECT station_id, hour, arrivals, departures
           FROM station_hourly_flow
           WHERE hour >= %(start)s AND hour < %(end)s;""",
        conn, params={"start": w_start, "end": w_end})
    flow_legacy["station_id"] = flow_legacy["station_id"].str.replace(
        r"\.0$", "", regex=True)
    flow = (pd.concat([flow_modern, flow_legacy], ignore_index=True)
              .drop_duplicates(["station_id", "hour"]))
    flow["hour"] = pd.to_datetime(flow["hour"], utc=True)
    flow["net_flow"] = flow["arrivals"] - flow["departures"]
    return flow[["station_id", "hour", "net_flow"]]


# ---------------------------------------------------------------------------
# UPDATE via staging temp table
# ---------------------------------------------------------------------------

def update_via_temp(conn, df: pd.DataFrame) -> int:
    """COPY df into a TEMP staging table, then UPDATE training_features in one
    statement hitting ALL 6 horizon rows per (station_id, timestamp)."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)

    stage_col_defs = (
        'station_id VARCHAR(50), "timestamp" TIMESTAMPTZ, '
        + ", ".join(f"{c} DOUBLE PRECISION" for c in NEW_COLUMNS)
    )
    col_list = ", ".join(
        f'"{c}"' if c == "timestamp" else c for c in STAGE_COLS
    )
    set_clause = ", ".join(f"{c} = s.{c}" for c in NEW_COLUMNS)

    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TEMP TABLE _patch_stage ({stage_col_defs}) ON COMMIT DROP;"
        )
        cur.copy_expert(
            f"COPY _patch_stage ({col_list}) FROM STDIN WITH (FORMAT csv, NULL '\\N')",
            buf,
        )
        cur.execute(f"""
            UPDATE training_features tf
            SET {set_clause}
            FROM _patch_stage s
            WHERE tf.station_id = s.station_id
              AND tf."timestamp" = s."timestamp";
        """)
        n = cur.rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Per-month patch
# ---------------------------------------------------------------------------

def patch_month(conn, month_start: date, cumflow_lookup: pd.DataFrame):
    if GAP_START <= month_start < GAP_END or month_start.year == 2020:
        print(f"  {month_start:%Y-%m}  SKIP (gap / excluded year)", flush=True)
        return

    m_start = pd.Timestamp(month_start, tz="UTC")
    m_end   = m_start + pd.DateOffset(months=1)
    t0 = time.time()

    df = load_month_timestamps(conn, m_start, m_end)
    if df.empty:
        print(f"  {month_start:%Y-%m}  no rows in training_features", flush=True)
        return
    print(f"  {month_start:%Y-%m}  {len(df):,} unique station-hours", flush=True)

    # 1. Cyclical time encodings (pure math)
    df["hour_sin"]  = np.sin(2 * math.pi * df["hour_of_day"] / 24)
    df["hour_cos"]  = np.cos(2 * math.pi * df["hour_of_day"] / 24)
    df["dow_sin"]   = np.sin(2 * math.pi * df["day_of_week"] / 7)
    df["dow_cos"]   = np.cos(2 * math.pi * df["day_of_week"] / 7)
    df["month_sin"] = np.sin(2 * math.pi * df["month"] / 12)
    df["month_cos"] = np.cos(2 * math.pi * df["month"] / 12)

    # 2. Cumulative expected net flow (merge precomputed lookup)
    df = df.merge(
        cumflow_lookup,
        on=["station_id", "hour_of_day", "day_of_week"],
        how="left")

    # 3. Net-flow momentum lags from station_hourly_flow.
    #    For lag L hours: the net flow that was observed at timestamp - L.
    #    Shift each flow row's hour FORWARD by L so it joins on the later timestamp.
    buf_start = m_start - pd.Timedelta(hours=max(FLOW_LAG_HOURS))
    flow = load_flow_dual(conn, buf_start, m_end)

    for lag_h in FLOW_LAG_HOURS:
        col = f"net_flow_{lag_h}hr"
        lag_flow = flow[["station_id", "hour", "net_flow"]].copy()
        lag_flow = lag_flow.rename(columns={
            "net_flow": col,
            "hour": "timestamp",
        })
        lag_flow["timestamp"] = lag_flow["timestamp"] + pd.Timedelta(hours=lag_h)
        df = df.merge(lag_flow, on=["station_id", "timestamp"], how="left")

    n_updated = update_via_temp(conn, df[STAGE_COLS])
    print(
        f"  {month_start:%Y-%m}  done {time.time()-t0:.0f}s  "
        f"unique_ts={len(df):,}  rows_updated={n_updated:,}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Multiprocessing worker
# ---------------------------------------------------------------------------

def _worker(args):
    """Top-level worker — must be picklable (no lambdas).
    Each worker opens its own DB connection and processes its month slice.
    cumflow_lookup is pickled once per worker at pool startup (~few MB)."""
    months, cumflow_lookup = args
    conn = get_conn()
    try:
        for month_start in months:
            patch_month(conn, month_start, cumflow_lookup)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Patch training_features with 14 Tier-1 feature columns.")
    ap.add_argument("--start", type=parse_month,
                    help="first month to patch, YYYY-MM")
    ap.add_argument("--end",   type=parse_month,
                    help="last month to patch, YYYY-MM (inclusive)")
    ap.add_argument("--alter-only", action="store_true",
                    help="just run ALTER TABLE to add columns, then exit")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel worker processes (default 1). "
                         "Each worker gets its own DB connection.")
    args = ap.parse_args()

    conn = get_conn()
    try:
        alter_table(conn)
        if args.alter_only:
            return
        if not (args.start and args.end):
            ap.error("--start and --end are required unless --alter-only")

        print("Building cumulative net-flow lookup...", flush=True)
        cumflow_lookup = build_cumflow_lookup(conn)
        month_list = list(months_between(args.start, args.end))
        print(f"Patching {len(month_list)} months with {args.workers} worker(s)...",
              flush=True)
    finally:
        conn.close()

    t0 = time.time()
    if args.workers <= 1:
        conn = get_conn()
        try:
            for m in month_list:
                patch_month(conn, m, cumflow_lookup)
        finally:
            conn.close()
    else:
        n_workers = min(args.workers, len(month_list))
        chunks = [month_list[i::n_workers] for i in range(n_workers)]
        tasks  = [(chunk, cumflow_lookup) for chunk in chunks]
        with multiprocessing.Pool(n_workers) as pool:
            pool.map(_worker, tasks)

    print(f"Done. {len(month_list)} months in {time.time()-t0:.0f}s.", flush=True)


if __name__ == "__main__":
    main()

"""Phase 2 (clean stage) — build station_status_hourly_clean.

This is STEP 1+2 of the cleaning pipeline:

    station_status / station_status_pre2021   (raw, read-only)
            |  ① retrieve one month, ② clean_month() in pandas, sample to hourly
            v
    station_status_hourly_clean                (COPY the cleaned month in)

Cleaning runs in PANDAS, one month at a time (a single hourly-sampled month is
~1.4M rows — fits comfortably in RAM; the raw 334M-row archive does not). The
cleaned rows are bulk-written with psycopg2 COPY (orders of magnitude faster
than row INSERTs), so a full backfill is a one-time cost of tens of minutes.

Cleaning is ROW-LOCAL (each row judged on its own), so NO cross-month buffer is
needed here. The buffer only matters in the *feature* build downstream, where
lag windows look backward across month boundaries.

The actual cleaning RULES are prototyped in notebooks/0.01-eda-data-quality.py
against real data. The known-safe rules live in clean_month() below; port any
new rules the EDA turns up into that one function (single source of truth).

Usage (run from project root so `citibike` imports):
    python model_training/build_clean_availability.py --start 2026-05 --end 2026-06
    python model_training/build_clean_availability.py --start 2016-01 --end 2021-12
    python model_training/build_clean_availability.py --create-only   # just DDL
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
from citibike.config import DB_CONFIG  # noqa: E402

DDL_PATH = Path(__file__).resolve().parents[1] / "sql" / "station_status_hourly_clean.sql"

# Columns written to station_status_hourly_clean (order matters for COPY).
CLEAN_COLUMNS = [
    "station_id", "hour",
    "num_bikes_available", "num_ebikes_available",
    "num_docks_available", "num_bikes_disabled", "num_docks_disabled",
    "is_installed", "is_renting", "is_returning",
    "capacity",
]

# Same gap/exclusion logic as the feature builder: no raw snapshots exist in the
# 2022-April 2026 gap, and 2020 is excluded (COVID anomaly) per the project spec.
GAP_START = date(2022, 1, 1)
GAP_END = date(2026, 5, 1)  # exclusive — May 2026 onward has live status data


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def create_table(conn):
    """Run the CREATE TABLE / hypertable DDL (idempotent)."""
    conn.cursor().execute(DDL_PATH.read_text())
    conn.commit()
    print(f"Ensured station_status_hourly_clean exists (from {DDL_PATH.name}).")


def status_table_for_month(month_start: date):
    """Return the raw status table for a month, or None if it's in the gap / 2020."""
    if GAP_START <= month_start < GAP_END:
        return None
    if month_start.year == 2020:
        return None
    return "station_status_pre2021" if month_start.year <= 2021 else "station_status"


def months_between(start: date, end: date):
    cur = start.replace(day=1)
    last = end.replace(day=1)
    while cur <= last:
        yield cur
        cur += relativedelta(months=1)


def load_month(conn, status_tbl: str, m_start: date, m_end: date) -> pd.DataFrame:
    """Pull one raw month of availability snapshots into a DataFrame."""
    sql = f"""
        SELECT fetched_at, station_id,
               num_bikes_available, num_ebikes_available,
               num_docks_available, num_bikes_disabled, num_docks_disabled,
               is_installed, is_renting, is_returning
        FROM {status_tbl}
        WHERE fetched_at >= %(m_start)s AND fetched_at < %(m_end)s;
    """
    return pd.read_sql(sql, conn, params={"m_start": m_start, "m_end": m_end})


def clean_month(df: pd.DataFrame, capacity: pd.Series) -> pd.DataFrame:
    """Apply Section-A cleaning rules to one raw month, then point-sample hourly.

    Cleaning is row-local — no cross-month buffer needed. Rules here are the
    known-safe ones; extend from the 0.01 EDA notebook as new issues surface.
    """
    out = df.copy()

    # Merge capacity (from station_information) so we can range-check against it.
    out = out.merge(capacity.rename("capacity"), on="station_id", how="left")

    # --- drop exact duplicate snapshots ---
    out = out.drop_duplicates(subset=["station_id", "fetched_at"])

    # --- impossible values: drop negatives only ---
    # (0.01 EDA on May 2026 found 0 negatives, but pre-2021 Kaggle data can have
    #  parse glitches, so the guard stays.)
    count_cols = ["num_bikes_available", "num_ebikes_available",
                  "num_docks_available", "num_bikes_disabled", "num_docks_disabled"]
    out = out[(out[count_cols].fillna(0) >= 0).all(axis=1)]

    # NOTE: bikes > capacity is NOT dropped (decision 2026-06-15). The 0.01 EDA
    # showed all 8,650 such rows (May 2026) are at INSTALLED stations with real
    # positive capacity — genuine rebalancing overfill, not junk. Keep them as-is;
    # fill_ratio is allowed to exceed 1.0 downstream (XGBoost is fine; linear sees
    # a rare >1). Dropping them would discard valid operational data.

    # NOTE: non-operational stations (is_installed = 0, ~4.2% of May 2026 rows) are
    # also KEPT (decision 2026-06-15). capacity <= 0 is a perfect subset of these,
    # and the feature builder's NULLIF(capacity, 0) already yields NULL fill_ratio /
    # normalized features for them; raw zero counts still flow into lags/targets.

    # --- coerce is_* to clean booleans (Kaggle loader stored some as 1.0/0.0/NaN) ---
    for c in ["is_installed", "is_renting", "is_returning"]:
        out[c] = out[c].map({1: True, 1.0: True, "1": True, "t": True, True: True,
                             0: False, 0.0: False, "0": False, "f": False, False: False})

    # TODO (from EDA): stuck-sensor flagging — deferred until a FULL month is
    # available; the 0.01 EDA window was only ~2 days (live ingest started
    # 2026-05-05), too short to distinguish a stuck sensor from a low-traffic dock.

    # --- point-sample one snapshot per (station_id, hour): keep the LAST in the
    #     hour (closest to the hour boundary), matching the SQL DISTINCT ON. ---
    out["fetched_at"] = pd.to_datetime(out["fetched_at"], utc=True)
    out["hour"] = out["fetched_at"].dt.floor("h")
    out = (out.sort_values("fetched_at")
              .drop_duplicates(subset=["station_id", "hour"], keep="last"))

    # --- coerce integer columns to pandas nullable Int64 so COPY gets "31"/"\N",
    #     never "31.0". A NaN (orphan station with no capacity match, or pre-2018
    #     NULL ebikes) otherwise promotes the whole column to float64, and to_csv
    #     writes "31.0" which Postgres rejects for an INTEGER column. ---
    int_cols = ["num_bikes_available", "num_ebikes_available", "num_docks_available",
                "num_bikes_disabled", "num_docks_disabled", "capacity"]
    out[int_cols] = out[int_cols].astype("Int64")

    return out[CLEAN_COLUMNS]


def copy_into_clean(conn, df: pd.DataFrame, fast: bool = False) -> int:
    """Bulk-write a cleaned month into station_status_hourly_clean via COPY.

    fast=False (default): COPY → temp staging → INSERT ON CONFLICT DO NOTHING.
    fast=True: COPY directly, synchronous_commit=off. Use after TRUNCATE.
    """
    if df.empty:
        return 0
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)

    cols = ", ".join(CLEAN_COLUMNS)
    with conn.cursor() as cur:
        if fast:
            cur.execute("SET synchronous_commit = off;")
            cur.copy_expert(
                f"COPY station_status_hourly_clean ({cols}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf
            )
            n = cur.rowcount
        else:
            cur.execute(
                "CREATE TEMP TABLE _clean_stage "
                "(LIKE station_status_hourly_clean INCLUDING DEFAULTS) ON COMMIT DROP;"
            )
            cur.copy_expert(
                f"COPY _clean_stage ({cols}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf
            )
            cur.execute(
                f"INSERT INTO station_status_hourly_clean ({cols}) "
                f"SELECT {cols} FROM _clean_stage "
                "ON CONFLICT (station_id, hour) DO NOTHING;"
            )
            n = cur.rowcount
    conn.commit()
    return n


def build_month(conn, month_start: date, capacity: pd.Series, fast: bool = False):
    status_tbl = status_table_for_month(month_start)
    if status_tbl is None:
        print(f"  {month_start:%Y-%m}  SKIP (gap / excluded year)", flush=True)
        return
    m_end = month_start + relativedelta(months=1)
    t0 = time.time()

    raw = load_month(conn, status_tbl, month_start, m_end)
    cleaned = clean_month(raw, capacity)
    n = copy_into_clean(conn, cleaned, fast=fast)
    print(f"  {month_start:%Y-%m}  {time.time()-t0:.0f}s  raw={len(raw):,}  "
          f"clean/hourly={len(cleaned):,}  inserted={n:,}  (src={status_tbl})", flush=True)


def _worker(args):
    """Top-level worker for multiprocessing.Pool."""
    months, capacity, fast = args
    conn = get_conn()
    try:
        for month_start in months:
            build_month(conn, month_start, capacity, fast)
    finally:
        conn.close()


def parse_month(s: str) -> date:
    return datetime.strptime(s, "%Y-%m").date().replace(day=1)


def load_capacity(conn) -> pd.Series:
    """One capacity value per station, indexed by station_id."""
    cap = pd.read_sql("SELECT station_id, capacity FROM station_information;", conn)
    return cap.set_index("station_id")["capacity"]


def main():
    ap = argparse.ArgumentParser(description="Build station_status_hourly_clean.")
    ap.add_argument("--start", type=parse_month, help="first month, YYYY-MM")
    ap.add_argument("--end", type=parse_month, help="last month, YYYY-MM (inclusive)")
    ap.add_argument("--create-only", action="store_true", help="just run the DDL and exit")
    ap.add_argument("--fast", action="store_true",
                    help="direct COPY + synchronous_commit=off. Use for backfill after TRUNCATE.")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel worker processes (default 1). Set to CPU count for backfill.")
    args = ap.parse_args()

    conn = get_conn()
    try:
        create_table(conn)
        if args.create_only:
            return
        if not (args.start and args.end):
            ap.error("--start and --end are required unless --create-only")

        capacity = load_capacity(conn)
        month_list = list(months_between(args.start, args.end))

        if args.workers <= 1:
            for m in month_list:
                build_month(conn, m, capacity, args.fast)
        else:
            n_workers = min(args.workers, len(month_list))
            chunks = [month_list[i::n_workers] for i in range(n_workers)]
            tasks = [(chunk, capacity, args.fast) for chunk in chunks]
            t0 = time.time()
            with multiprocessing.Pool(n_workers) as pool:
                pool.map(_worker, tasks)
            print(f"Done. {len(month_list)} months in {time.time()-t0:.0f}s ({n_workers} workers).")
            return
    finally:
        conn.close()
    print("Done.")


if __name__ == "__main__":
    main()

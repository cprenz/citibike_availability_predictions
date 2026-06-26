"""Phase 2 (clean stage) — build station_status_hourly_clean.

    station_status / station_status_pre2021  (raw, read-only)
            |  clean_month() in pandas, point-sample to hourly
            v
    station_status_hourly_clean              (COPY the cleaned month in)

I clean one month at a time (~1.4M rows after hourly sampling — fits in RAM).
Rows are bulk-written via psycopg2 COPY; a full backfill takes tens of minutes.

Cleaning is row-local so no cross-month buffer is needed here. The buffer only
matters in the feature build downstream, where lag windows cross month boundaries.

All cleaning rules live in clean_month() — that's the single source of truth.
New rules discovered in notebooks/0.01-eda-data-quality.py get ported in there.

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

CLEAN_COLUMNS = [  # order matters for COPY
    "station_id", "hour",
    "num_bikes_available", "num_ebikes_available",
    "num_docks_available", "num_bikes_disabled", "num_docks_disabled",
    "is_installed", "is_renting", "is_returning",
    "capacity",
]

GAP_START = date(2022, 1, 1)
GAP_END = date(2026, 5, 1)  # exclusive — May 2026 onward has live status data


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def create_table(conn):
    """Create station_status_hourly_clean if it doesn't exist (idempotent)."""
    conn.cursor().execute(DDL_PATH.read_text())
    conn.commit()
    print(f"Ensured station_status_hourly_clean exists (from {DDL_PATH.name}).")


def status_table_for_month(month_start: date):
    """Return the source status table for this month, or None to skip (gap / 2020)."""
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
    """Pull one raw month of availability snapshots."""
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
    """Apply cleaning rules to one raw month and point-sample to hourly.

    Rules here are the known-safe ones from the 0.01 EDA. Extend this function
    (not the callers) as new issues surface.
    """
    out = df.copy()

    # Merge capacity (from station_information) so we can range-check against it.
    out = out.merge(capacity.rename("capacity"), on="station_id", how="left")

    out = out.drop_duplicates(subset=["station_id", "fetched_at"])

    # Drop negatives only. The 0.01 EDA found 0 in May 2026, but the Kaggle archive
    # can have parse glitches so the guard stays.
    count_cols = ["num_bikes_available", "num_ebikes_available",
                  "num_docks_available", "num_bikes_disabled", "num_docks_disabled"]
    out = out[(out[count_cols].fillna(0) >= 0).all(axis=1)]

    # bikes > capacity: NOT dropped (decision 2026-06-15). All 8,650 such rows in
    # May 2026 are at INSTALLED stations — genuine rebalancing overfill, not junk.
    # fill_ratio can exceed 1.0 downstream; that's fine.

    # is_installed=0 and capacity<=0: also KEPT (decision 2026-06-15). The feature
    # builder's NULLIF(capacity,0) handles them; raw counts still feed lags/targets.

    # Coerce is_* to clean booleans — the Kaggle loader stored some as 1.0/0.0/NaN.
    for c in ["is_installed", "is_renting", "is_returning"]:
        out[c] = out[c].map({1: True, 1.0: True, "1": True, "t": True, True: True,
                             0: False, 0.0: False, "0": False, "f": False, False: False})

    # TODO: stuck-sensor flagging — deferred. The 0.01 EDA window was only ~2 days
    # (live ingest started 2026-05-05), too short to tell a stuck sensor from a
    # low-traffic dock. Revisit once a full month is available.

    # Point-sample: one snapshot per (station_id, hour), keeping the last in the hour
    # (closest to the boundary) — matches the SQL builder's DISTINCT ON.
    out["fetched_at"] = pd.to_datetime(out["fetched_at"], utc=True)
    out["hour"] = out["fetched_at"].dt.floor("h")
    out = (out.sort_values("fetched_at")
              .drop_duplicates(subset=["station_id", "hour"], keep="last"))

    # Cast to nullable Int64 so COPY gets "31"/"\N", never "31.0". A single NaN
    # (orphan station or pre-2018 NULL ebike) promotes the whole column to float64,
    # and to_csv writes "31.0" which Postgres rejects for an INTEGER column.
    int_cols = ["num_bikes_available", "num_ebikes_available", "num_docks_available",
                "num_bikes_disabled", "num_docks_disabled", "capacity"]
    out[int_cols] = out[int_cols].astype("Int64")

    return out[CLEAN_COLUMNS]


def copy_into_clean(conn, df: pd.DataFrame, fast: bool = False) -> int:
    """Bulk-write a cleaned month into station_status_hourly_clean via COPY.

    fast=False: COPY -> temp staging -> INSERT ON CONFLICT DO NOTHING (safe for reruns).
    fast=True: COPY directly with synchronous_commit=off. Only safe after a TRUNCATE.
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
    """Multiprocessing.Pool worker — must be top-level to pickle."""
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
    """Current capacity per station from station_information, indexed by station_id."""
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

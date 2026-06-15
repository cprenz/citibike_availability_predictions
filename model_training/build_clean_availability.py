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
import sys
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

    # --- impossible values: negatives and bikes beyond capacity ---
    count_cols = ["num_bikes_available", "num_ebikes_available",
                  "num_docks_available", "num_bikes_disabled", "num_docks_disabled"]
    out = out[(out[count_cols].fillna(0) >= 0).all(axis=1)]
    over_cap = out["num_bikes_available"] > out["capacity"]
    out = out[~over_cap.fillna(False)]

    # --- coerce is_* to clean booleans (Kaggle loader stored some as 1.0/0.0/NaN) ---
    for c in ["is_installed", "is_renting", "is_returning"]:
        out[c] = out[c].map({1: True, 1.0: True, "1": True, "t": True, True: True,
                             0: False, 0.0: False, "0": False, "f": False, False: False})

    # TODO (from EDA): stuck-sensor flagging, null policy per column, tz checks.

    # --- point-sample one snapshot per (station_id, hour): keep the LAST in the
    #     hour (closest to the hour boundary), matching the SQL DISTINCT ON. ---
    out["hour"] = out["fetched_at"].dt.floor("h")
    out = (out.sort_values("fetched_at")
              .drop_duplicates(subset=["station_id", "hour"], keep="last"))

    return out[CLEAN_COLUMNS]


def copy_into_clean(conn, df: pd.DataFrame) -> int:
    """Bulk-write a cleaned month into station_status_hourly_clean via COPY.

    COPY is ~10-100x faster than row INSERTs. We COPY into a TEMP staging table
    then INSERT ... ON CONFLICT DO NOTHING so re-running a month is idempotent.
    """
    if df.empty:
        return 0
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)

    cols = ", ".join(CLEAN_COLUMNS)
    with conn.cursor() as cur:
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


def build_month(conn, month_start: date, capacity: pd.Series):
    status_tbl = status_table_for_month(month_start)
    if status_tbl is None:
        print(f"  {month_start:%Y-%m}  SKIP (gap / excluded year)")
        return
    m_end = month_start + relativedelta(months=1)

    raw = load_month(conn, status_tbl, month_start, m_end)
    cleaned = clean_month(raw, capacity)
    n = copy_into_clean(conn, cleaned)
    print(f"  {month_start:%Y-%m}  raw={len(raw):,}  ->  clean/hourly={len(cleaned):,}  "
          f"inserted={n:,}  (src={status_tbl})")


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
    args = ap.parse_args()

    conn = get_conn()
    try:
        create_table(conn)
        if args.create_only:
            return
        if not (args.start and args.end):
            ap.error("--start and --end are required unless --create-only")

        capacity = load_capacity(conn)
        for m in months_between(args.start, args.end):
            build_month(conn, m, capacity)
        print("Done.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

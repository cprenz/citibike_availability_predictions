"""Validate the pandas feature builder against the SQL builder (Phase 2, step 2).

Compares two populations of training_features rows that were built by the two
different builders into two different tables:

    training_features          <- build_training_features.py        (SQL, reference)
    training_features_pandas   <- build_training_features_pandas.py (Option B, new)

It does three things, all in-DB (one aggregation pass — no pandas pull):

  1. ROW OVERLAP on the PK (station_id, timestamp, horizon_minutes): how many rows
     are in both, only in SQL, only in pandas. The pandas builder uses exact-key
     targets (NaN at a missing hour) while the SQL builder allowed a +2h forward
     tolerance, so SQL is EXPECTED to have a few extra rows pandas doesn't. Those
     never enter the value comparison (we only compare the inner-join rows).

  2. PER-COLUMN MISMATCH counts over the inner-join rows. NULL-aware via IS DISTINCT
     FROM; float columns use abs(a-b) > FLOAT_TOL to ignore representation noise.

  3. A verdict per column. EXPECTED-nonzero columns are the lag / change / rolling
     features (and anything derived from them): at genuine hourly gaps the SQL
     builder borrowed the most-recent earlier row while pandas yields NULL, so a
     small mismatch count there is the documented edge difference, not a bug.
     Everything else should be 0.

Usage (run from project root):
    python model_training/validate_pandas_vs_sql.py
    python model_training/validate_pandas_vs_sql.py --sql-table training_features \
        --pandas-table training_features_pandas --tol 1e-6
"""

import argparse
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citibike.config import DB_CONFIG  # noqa: E402
from model_training.build_training_features_pandas import INSERT_COLUMNS  # noqa: E402

PK = ["station_id", "timestamp", "horizon_minutes"]

# Columns compared with a float tolerance (DOUBLE PRECISION in the DDL). Everything
# else is compared exactly with IS DISTINCT FROM (ints, bools, text, the targets).
FLOAT_COLS = {
    "fill_ratio", "fill_ratio_change_1hr", "rolling_mean_fill_ratio_6hr",
    "rate_of_change_10min", "rate_of_change_20min", "rate_of_change_30min",
    "temperature_2m", "apparent_temperature", "precipitation", "rain",
    "snowfall", "wind_speed_10m", "cloud_cover", "relative_humidity_2m",
    "forecast_temperature_2m", "forecast_apparent_temperature", "forecast_precipitation",
    "forecast_rain", "forecast_snowfall", "forecast_wind_speed_10m",
    "forecast_cloud_cover", "forecast_relative_humidity_2m",
    "nearest_entrance_dist_m", "member_ratio", "ebike_ratio",
    "avg_departures_this_hour_dow", "avg_arrivals_this_hour_dow", "avg_net_flow_this_hour_dow",
}

# Columns where a SMALL nonzero mismatch is EXPECTED (exact-key vs the SQL builder's
# fuzzy at/before lag lookup, which diverge only at genuine hourly gaps).
EXPECTED_NONZERO = {
    "bikes_1hr_ago", "bikes_3hr_ago", "bikes_6hr_ago", "bikes_12hr_ago",
    "bikes_same_hour_yesterday",
    "fill_ratio_change_1hr", "rolling_mean_fill_ratio_6hr",
    "change_bikes_1hr", "change_bikes_3hr", "change_bikes_6hr", "change_bikes_12hr",
    "change_ebikes_1hr", "change_ebikes_3hr", "change_ebikes_6hr", "change_ebikes_12hr",
    "change_classic_1hr", "change_classic_3hr", "change_classic_6hr", "change_classic_12hr",
}


def q(col: str) -> str:
    """Quote a column for SQL (timestamp is a reserved word)."""
    return f'"{col}"'


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def overlap_counts(cur, sql_tbl, pd_tbl):
    on = " AND ".join(f"a.{q(c)} = b.{q(c)}" for c in PK)
    cur.execute(f"SELECT count(*) FROM {sql_tbl};")
    n_sql = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {pd_tbl};")
    n_pd = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {sql_tbl} a JOIN {pd_tbl} b ON {on};")
    n_both = cur.fetchone()[0]
    return n_sql, n_pd, n_both


def column_mismatches(cur, sql_tbl, pd_tbl, compare_cols, tol):
    """One aggregation over the inner join: a mismatch count per column."""
    on = " AND ".join(f"a.{q(c)} = b.{q(c)}" for c in PK)
    exprs = []
    for c in compare_cols:
        a, b = f"a.{q(c)}", f"b.{q(c)}"
        if c in FLOAT_COLS:
            # NULL-mismatch OR numeric difference beyond tolerance
            cond = (f"(({a} IS NULL) <> ({b} IS NULL)) "
                    f"OR (abs(coalesce({a},0) - coalesce({b},0)) > {tol})")
        else:
            cond = f"{a} IS DISTINCT FROM {b}"
        exprs.append(f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS {q(c)}")
    sql = f"SELECT {', '.join(exprs)} FROM {sql_tbl} a JOIN {pd_tbl} b ON {on};"
    cur.execute(sql)
    row = cur.fetchone()
    return dict(zip(compare_cols, row))


def main():
    ap = argparse.ArgumentParser(description="Diff pandas vs SQL training_features.")
    ap.add_argument("--sql-table", default="training_features")
    ap.add_argument("--pandas-table", default="training_features_pandas")
    ap.add_argument("--tol", type=float, default=1e-6, help="float compare tolerance")
    args = ap.parse_args()

    compare_cols = [c for c in INSERT_COLUMNS if c not in PK]

    conn = get_conn()
    try:
        cur = conn.cursor()
        n_sql, n_pd, n_both = overlap_counts(cur, args.sql_table, args.pandas_table)
        print("ROW OVERLAP (on PK station_id, timestamp, horizon_minutes)")
        print(f"  {args.sql_table:<28} {n_sql:>12,}")
        print(f"  {args.pandas_table:<28} {n_pd:>12,}")
        print(f"  in both (compared)          {n_both:>12,}")
        print(f"  only in SQL (sql - both)    {n_sql - n_both:>12,}")
        print(f"  only in pandas (pd - both)  {n_pd - n_both:>12,}")
        print()

        mism = column_mismatches(cur, args.sql_table, args.pandas_table, compare_cols, args.tol)
        pct = lambda n: (100.0 * n / n_both) if n_both else 0.0  # noqa: E731

        print(f"PER-COLUMN MISMATCHES over {n_both:,} common rows (tol={args.tol})")
        print(f"  {'column':<36} {'mismatches':>12} {'pct':>8}   verdict")
        unexpected = 0
        for c in compare_cols:
            n = mism[c] or 0
            if n == 0:
                verdict = "ok"
            elif c in EXPECTED_NONZERO:
                verdict = "expected (gap-boundary lag)"
            else:
                verdict = "*** UNEXPECTED ***"
                unexpected += 1
            if n or verdict != "ok":
                print(f"  {c:<36} {n:>12,} {pct(n):>7.3f}%   {verdict}")

        print()
        clean = all(v == 0 for v in mism.values())
        if clean:
            print("RESULT: every compared column matches exactly. ✅")
        elif unexpected == 0:
            print("RESULT: all nonzero mismatches are in EXPECTED gap-boundary lag "
                  "columns. ✅ (no unexpected divergence)")
        else:
            print(f"RESULT: {unexpected} column(s) diverge UNEXPECTEDLY — investigate. ❌")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

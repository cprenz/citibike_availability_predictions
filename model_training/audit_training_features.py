"""Training-readiness audit for a training_features table (Phase 2 gate).

Answers one question: is this table clean enough to fit sklearn models on?
The crash/corruption checklist is CLAUDE.md Section A. Linear/Ridge/Logistic
THROW on NaN/inf; XGBoost tolerates NaN. So the audit reports, per the table:

  1. Target health   — NULL targets are useless and must be 0 (the build drops
     them; this confirms it).
  2. NaN / NULL per feature column — what a linear-model imputer MUST cover. NaNs
     here are legitimate (lags at hourly gaps, weather gaps, unmatched static), not
     corruption — but they still crash a raw LinearRegression.fit().
  3. inf / -inf in float columns — silently break StandardScaler and explode Ridge.
  4. Constant (zero-variance) columns — break per-fold scaling (÷0), add nothing.
  5. Impossible values — negative counts; flagged for review.
  6. Non-numeric feature columns (season, station_role) — need encoding before a
     numeric model (a pipeline concern, not dirtiness).

Run from project root:
    python model_training/audit_training_features.py
    python model_training/audit_training_features.py --training-years-only
    python model_training/audit_training_features.py --table training_features_pandas
"""

import argparse
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citibike.config import DB_CONFIG  # noqa: E402
from model_training.build_training_features_pandas import (  # noqa: E402
    INSERT_COLUMNS, INT64_COLUMNS, BOOL_COLUMNS,
)

PK = ["station_id", "timestamp", "horizon_minutes"]
TARGETS = ["bikes_available_at_horizon", "bike_available_binary"]
TEXT_COLS = ["season", "station_role"]
# Integer count columns that must never be negative (impossible-value check).
NONNEG_INT = ["num_bikes_available", "num_ebikes_available", "num_docks_available",
              "num_bikes_disabled", "capacity", "departures_this_hour", "arrivals_this_hour"]


def q(col):
    return f'"{col}"' if col == "timestamp" else col


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def scalar(cur, sql):
    cur.execute(sql)
    return cur.fetchone()


def main():
    ap = argparse.ArgumentParser(description="Audit a training_features table for sklearn readiness.")
    ap.add_argument("--table", default="training_features")
    ap.add_argument("--training-years-only", action="store_true",
                    help="Filter to training years 2019, 2021, 2026 only")
    args = ap.parse_args()
    t = args.table
    where = "WHERE EXTRACT(YEAR FROM \"timestamp\") IN (2019, 2021, 2026)" if args.training_years_only else ""
    year_label = " (training years: 2019, 2021, 2026)" if args.training_years_only else ""

    feature_cols = [c for c in INSERT_COLUMNS if c not in PK and c not in TARGETS]
    numeric_feats = [c for c in feature_cols if c not in TEXT_COLS and c not in BOOL_COLUMNS]
    # float = numeric feature that isn't a nullable-int or bool column.
    float_feats = [c for c in numeric_feats if c not in INT64_COLUMNS]

    conn = get_conn()
    try:
        cur = conn.cursor()
        (n_rows,) = scalar(cur, f"SELECT count(*) FROM {t} {where};")
        print(f"TABLE {t}{year_label}: {n_rows:,} rows\n")

        # 1. target health
        print("1. TARGET HEALTH (must be 0 NULLs)")
        for tg in TARGETS:
            base = f"FROM {t} {where}"
            and_ = "AND" if where else "WHERE"
            (nn,) = scalar(cur, f"SELECT count(*) {base} {and_} {tg} IS NULL;")
            flag = "ok" if nn == 0 else "*** NULL TARGETS ***"
            print(f"   {tg:<32} nulls={nn:>10,}   {flag}")
        print()

        # 2. NaN / NULL per feature column (only nonzero, sorted desc)
        print("2. NULL/NaN PER FEATURE COLUMN (an imputer must cover these for linear models)")
        null_exprs = ", ".join(
            f"SUM(CASE WHEN {q(c)} IS NULL THEN 1 ELSE 0 END) AS {c}" for c in feature_cols)
        cur.execute(f"SELECT {null_exprs} FROM {t} {where};")
        nulls = dict(zip(feature_cols, cur.fetchone()))
        any_null = False
        for c, n in sorted(nulls.items(), key=lambda kv: -(kv[1] or 0)):
            n = n or 0
            if n:
                any_null = True
                print(f"   {c:<36} {n:>11,}  {100.0*n/n_rows:>6.2f}%")
        if not any_null:
            print("   (none)")
        print()

        # 3. inf / -inf in float columns (NaN already counted as NULL by COPY)
        print("3. inf / -inf IN FLOAT COLUMNS")
        inf_exprs = ", ".join(
            f"SUM(CASE WHEN {c} = 'Infinity'::float8 OR {c} = '-Infinity'::float8 "
            f"THEN 1 ELSE 0 END) AS {c}" for c in float_feats)
        cur.execute(f"SELECT {inf_exprs} FROM {t} {where};")
        infs = dict(zip(float_feats, cur.fetchone()))
        any_inf = any((v or 0) for v in infs.values())
        if any_inf:
            for c, n in infs.items():
                if n:
                    print(f"   {c:<36} {n:>11,}  *** inf ***")
        else:
            print("   (none)")
        print()

        # 4. constant / zero-variance numeric columns (single pass over all columns)
        print("4. CONSTANT (zero-variance) NUMERIC COLUMNS")
        minmax_exprs = ", ".join(
            f"min({c}) AS mn_{i}, max({c}) AS mx_{i}, count({c}) AS cnt_{i}"
            for i, c in enumerate(numeric_feats)
        )
        cur.execute(f"SELECT {minmax_exprs} FROM {t} {where};")
        row = cur.fetchone()
        any_const = False
        for i, c in enumerate(numeric_feats):
            mn, mx, nonnull = row[i*3], row[i*3+1], row[i*3+2]
            if nonnull and mn is not None and mn == mx:
                any_const = True
                print(f"   {c:<36} constant = {mn}")
        if not any_const:
            print("   (none)")
        print()

        # 5. impossible negatives
        print("5. IMPOSSIBLE VALUES (negative counts)")
        any_neg = False
        for c in NONNEG_INT:
            and_ = "AND" if where else "WHERE"
            (nn,) = scalar(cur, f"SELECT count(*) FROM {t} {where} {and_} {c} < 0;")
            if nn:
                any_neg = True
                print(f"   {c:<36} negatives = {nn:,}  *** review ***")
        if not any_neg:
            print("   (none)")
        print()

        # 6. non-numeric feature columns (need encoding)
        print("6. NON-NUMERIC FEATURES (encode before a numeric model)")
        print(f"   text:    {TEXT_COLS}")
        print(f"   boolean: {BOOL_COLUMNS}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

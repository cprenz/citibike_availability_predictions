"""Lightweight audit of the 14 Tier-1 columns added by patch_tier1_features.py.

Three checks:
  1. NULL counts — cyclical must be 0%; cumflow/flow-lags must match reference columns.
  2. inf / -inf — none expected.
  3. Constant columns — all 14 must have real variance.

Usage:
    python model_training/audit_tier1_columns.py
"""

import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citibike.config import DB_CONFIG  # noqa: E402

TIER1_CYCLICAL = [
    "hour_sin", "hour_cos", "dow_sin", "dow_cos", "month_sin", "month_cos",
]
TIER1_CUMFLOW = [
    "cumulative_expected_net_flow_1hr", "cumulative_expected_net_flow_3hr",
    "cumulative_expected_net_flow_6hr", "cumulative_expected_net_flow_12hr",
    "cumulative_expected_net_flow_24hr",
]
TIER1_FLOW_LAGS = ["net_flow_1hr", "net_flow_3hr", "net_flow_6hr"]
TIER1_ALL = TIER1_CYCLICAL + TIER1_CUMFLOW + TIER1_FLOW_LAGS

TABLE = "training_features"
REF_CUMFLOW  = "avg_net_flow_this_hour_dow"
REF_FLOWLAGS = "departures_this_hour"


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    cur  = conn.cursor()

    cur.execute(f"SELECT count(*) FROM {TABLE};")
    n_rows = cur.fetchone()[0]
    print(f"Table: {TABLE}  total rows: {n_rows:,}\n")

    # Reference NULL counts
    cur.execute(f"SELECT count(*) FROM {TABLE} WHERE {REF_CUMFLOW} IS NULL;")
    ref_cf = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM {TABLE} WHERE {REF_FLOWLAGS} IS NULL;")
    ref_fl = cur.fetchone()[0]
    print(f"Reference NULLs (for comparison):")
    print(f"  {REF_CUMFLOW:<52} {ref_cf:>14,}  ({100.0*ref_cf/n_rows:.2f}%)")
    print(f"  {REF_FLOWLAGS:<52} {ref_fl:>14,}  ({100.0*ref_fl/n_rows:.2f}%)")

    # ------------------------------------------------------------------
    # 1. NULL counts — single table scan for all 14 columns
    # ------------------------------------------------------------------
    print("\n--- 1. NULL counts ---")
    null_exprs = ", ".join(
        f"SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) AS {c}"
        for c in TIER1_ALL
    )
    cur.execute(f"SELECT {null_exprs} FROM {TABLE};")
    null_counts = dict(zip(TIER1_ALL, cur.fetchone()))

    all_ok = True
    for c in TIER1_ALL:
        nn = null_counts[c] or 0
        pct = 100.0 * nn / n_rows
        if c in TIER1_CYCLICAL:
            status = "OK" if nn == 0 else "*** UNEXPECTED NULLs ***"
        elif c in TIER1_CUMFLOW:
            status = "OK" if abs(nn - ref_cf) / max(n_rows, 1) < 0.001 else "CHECK"
        else:
            status = "OK" if nn <= ref_fl * 1.10 else "CHECK"
        if status != "OK":
            all_ok = False
        print(f"  {c:<52} {nn:>14,}  {pct:5.2f}%  {status}")
    if all_ok:
        print("  => all NULL patterns as expected")

    # ------------------------------------------------------------------
    # 2. inf / -inf — single table scan for all 14 columns
    # ------------------------------------------------------------------
    print("\n--- 2. inf / -inf ---")
    inf_exprs = ", ".join(
        f"SUM(CASE WHEN {c} = 'Infinity'::float8 OR {c} = '-Infinity'::float8 "
        f"THEN 1 ELSE 0 END) AS {c}"
        for c in TIER1_ALL
    )
    cur.execute(f"SELECT {inf_exprs} FROM {TABLE};")
    inf_counts = dict(zip(TIER1_ALL, cur.fetchone()))
    found_inf = False
    for c, v in inf_counts.items():
        if v:
            print(f"  *** {c}: {v:,} inf values ***")
            found_inf = True
    if not found_inf:
        print("  (none) -- all 14 columns clean")

    # ------------------------------------------------------------------
    # 3. Constant columns — single table scan for all 14 min/max pairs
    # ------------------------------------------------------------------
    print("\n--- 3. Constant columns ---")
    minmax_exprs = ", ".join(
        f"min({c}), max({c})" for c in TIER1_ALL
    )
    cur.execute(f"SELECT {minmax_exprs} FROM {TABLE};")
    row = cur.fetchone()
    found_const = False
    for i, c in enumerate(TIER1_ALL):
        mn, mx = row[i * 2], row[i * 2 + 1]
        if mn is not None and mn == mx:
            print(f"  *** {c} is constant: {mn} ***")
            found_const = True
    if not found_const:
        print("  (none) -- all 14 columns have real variance")

    conn.close()
    print("\nAudit complete.")


if __name__ == "__main__":
    main()

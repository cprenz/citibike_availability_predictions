# %% [markdown]
# # 0.03 — training_features Audit (sklearn readiness)
#
# Answers one question: is `training_features` clean enough to fit models on?
#
# Six checks, in order of severity:
# 1. **Target health** — NULL targets poison metrics; must be 0.
# 2. **NaN / NULL per feature** — linear models throw on NaN; XGBoost tolerates it.
#    All NaNs here are *legitimate* (lags at hourly gaps, weather gaps, structural
#    proximity NULLs) — the audit just maps them so the trainer knows what to impute.
# 3. **inf / -inf** — silently break StandardScaler and explode Ridge.
# 4. **Constant columns** — zero-variance breaks per-fold scaling (÷0), adds nothing.
# 5. **Impossible negatives** — negative bike/dock counts are parse or sensor glitches.
# 6. **Non-numeric columns** — `season`, `station_role` need encoding before any
#    numeric model; booleans need explicit casting.
#
# Authored as a `# %%` .py file (clean git diffs). Export to `.ipynb` with outputs via:
#   Command Palette → "Jupyter: Export Current Python File as Jupyter Notebook"

# %%
import sys
from pathlib import Path

import pandas as pd
import psycopg2

# Make citibike package importable when run from notebooks/ or project root.
sys.path.insert(0, str(Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()))
from citibike.config import DB_CONFIG  # noqa: E402
from model_training.build_training_features_pandas import (  # noqa: E402
    INSERT_COLUMNS, INT64_COLUMNS, BOOL_COLUMNS,
)

pd.set_option("display.max_rows", 100)
pd.set_option("display.float_format", "{:,.2f}".format)

# %% [markdown]
# ## Configuration
# Change `TABLE` to audit a different table (e.g. `training_features_pandas`).

# %%
TABLE = "training_features"

PK      = ["station_id", "timestamp", "horizon_minutes"]
TARGETS = ["bikes_available_at_horizon", "bike_available_binary"]
TEXT_COLS  = ["season", "station_role"]
NONNEG_INT = [
    "num_bikes_available", "num_ebikes_available", "num_docks_available",
    "num_bikes_disabled", "capacity", "departures_this_hour", "arrivals_this_hour",
]

feature_cols  = [c for c in INSERT_COLUMNS if c not in PK and c not in TARGETS]
numeric_feats = [c for c in feature_cols if c not in TEXT_COLS and c not in BOOL_COLUMNS]
float_feats   = [c for c in numeric_feats if c not in INT64_COLUMNS]

def q(col):
    """Quote timestamp column name for SQL; leave others bare."""
    return f'"{col}"' if col == "timestamp" else col

conn = psycopg2.connect(**DB_CONFIG)
cur  = conn.cursor()

cur.execute(f"SELECT count(*) FROM {TABLE};")
n_rows = cur.fetchone()[0]
print(f"TABLE {TABLE}: {n_rows:,} rows")

# %% [markdown]
# ## 1. Target health
# Both targets must have zero NULLs. The feature builder drops NULL-target rows
# before writing, so any NULLs here indicate a builder bug.

# %%
rows = []
for tg in TARGETS:
    cur.execute(f"SELECT count(*) FROM {TABLE} WHERE {tg} IS NULL;")
    nn = cur.fetchone()[0]
    rows.append({"target": tg, "null_count": nn, "status": "✓ ok" if nn == 0 else "*** NULL TARGETS ***"})

pd.DataFrame(rows).set_index("target")

# %% [markdown]
# ## 2. NULL / NaN per feature column
# All NaNs listed here are *expected*:
# - Lag columns (`bikes_1hr_ago` etc.) — NaN at hourly gaps in the clean table.
# - Weather columns — coverage gaps; filled on next ingest run.
# - `nearest_entrance_dist_m` ~15% — structural (no subway within 800m); use a
#   sentinel / cap, never statistically impute (see CLAUDE.md Section B).
# - `rate_of_change_*` — 100% NULL; deferred sub-hourly feature; drop before training.
#
# XGBoost trains as-is. Linear / Ridge / Logistic need `SimpleImputer(median)`
# inside a per-fold `Pipeline` for the non-structural NaNs.

# %%
null_exprs = ", ".join(
    f"SUM(CASE WHEN {q(c)} IS NULL THEN 1 ELSE 0 END) AS {c.replace('.', '_')}"
    for c in feature_cols
)
cur.execute(f"SELECT {null_exprs} FROM {TABLE};")
raw = dict(zip(feature_cols, cur.fetchone()))

null_df = (
    pd.DataFrame([{"feature": c, "null_count": v or 0, "pct": 100.0 * (v or 0) / n_rows}
                  for c, v in raw.items()])
    .query("null_count > 0")
    .sort_values("null_count", ascending=False)
    .reset_index(drop=True)
)
null_df["null_count"] = null_df["null_count"].map("{:,}".format)
null_df["pct"] = null_df["pct"].map("{:.2f}%".format)
null_df

# %% [markdown]
# ## 3. inf / -inf in float columns
# Any row here breaks `StandardScaler` and causes Ridge to diverge.
# They come from divide-by-zero in ratio features (e.g. `fill_ratio` when
# `capacity = 0`). Should be none — the builder masks zero capacity to NaN first.

# %%
inf_exprs = ", ".join(
    f"SUM(CASE WHEN {c} = 'Infinity'::float8 OR {c} = '-Infinity'::float8 "
    f"THEN 1 ELSE 0 END) AS {c.replace('.', '_')}"
    for c in float_feats
)
cur.execute(f"SELECT {inf_exprs} FROM {TABLE};")
inf_raw = dict(zip(float_feats, cur.fetchone()))

inf_df = (
    pd.DataFrame([{"feature": c, "inf_count": v or 0} for c, v in inf_raw.items()])
    .query("inf_count > 0")
    .reset_index(drop=True)
)
if inf_df.empty:
    print("(none) — no inf / -inf values found")
else:
    inf_df

# %% [markdown]
# ## 4. Constant (zero-variance) columns
# A column with the same value in every row adds no signal and breaks
# `StandardScaler` (division by zero in the variance). Drop before training.
# Note: `snowfall` will appear constant on a summer-only window — check again
# after the full historical backfill covers winter months.

# %%
const_rows = []
for c in numeric_feats:
    cur.execute(f"SELECT min({c}), max({c}), count({c}) FROM {TABLE};")
    mn, mx, nonnull = cur.fetchone()
    if nonnull and mn is not None and mn == mx:
        const_rows.append({"feature": c, "constant_value": mn})

const_df = pd.DataFrame(const_rows)
if const_df.empty:
    print("(none) — no constant columns")
else:
    const_df

# %% [markdown]
# ## 5. Impossible values (negative counts)
# Negative bike / dock counts are impossible and indicate a parse glitch or
# Kaggle CSV artifact. Any flagged here should be investigated and the cleaning
# rule added to `clean_month()` in `build_clean_availability.py`.

# %%
neg_rows = []
for c in NONNEG_INT:
    cur.execute(f"SELECT count(*) FROM {TABLE} WHERE {c} < 0;")
    nn = cur.fetchone()[0]
    if nn:
        neg_rows.append({"feature": c, "negative_count": nn})

neg_df = pd.DataFrame(neg_rows)
if neg_df.empty:
    print("(none) — no negative counts")
else:
    neg_df

# %% [markdown]
# ## 6. Non-numeric feature columns
# These columns need encoding before any numeric model:
# - `season`, `station_role` → one-hot or ordinal encode
# - `is_weekend`, `is_holiday`, `is_within_400m` → cast to int (already 0/1,
#   but pandas `boolean` dtype needs explicit conversion for sklearn)

# %%
print("Text columns (one-hot or ordinal encode before numeric model):")
for c in TEXT_COLS:
    cur.execute(f"SELECT {c}, count(*) FROM {TABLE} GROUP BY {c} ORDER BY count(*) DESC;")
    vals = cur.fetchall()
    print(f"\n  {c}:")
    for v, n in vals:
        print(f"    {str(v):<20} {n:>12,} rows  ({100.0*n/n_rows:.1f}%)")

print(f"\nBoolean columns (cast to int before sklearn):")
for c in BOOL_COLUMNS:
    print(f"  {c}")

# %% [markdown]
# ## 7. Tier-1 new columns audit
# Spot-check of the 14 columns added by `patch_tier1_features.py`.
# Three checks:
# - **NULLs**: cyclical columns must be 0% NULL; cumulative net flow and net-flow
#   momentum lags must match the NULL footprint of their source column
#   (`avg_net_flow_this_hour_dow` and `departures_this_hour` respectively).
# - **inf / -inf**: sin/cos can't produce inf; cumulative sums and differences
#   shouldn't either — flag any that do.
# - **Constant columns**: all 14 should have real variance.

# %%
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

# Reference columns for NULL comparison
REF_CUMFLOW  = "avg_net_flow_this_hour_dow"
REF_FLOWLAGS = "departures_this_hour"

cur.execute(f"SELECT count(*) FROM {TABLE} WHERE {REF_CUMFLOW} IS NULL;")
ref_cumflow_nulls = cur.fetchone()[0]
cur.execute(f"SELECT count(*) FROM {TABLE} WHERE {REF_FLOWLAGS} IS NULL;")
ref_flowlag_nulls = cur.fetchone()[0]

print(f"Reference NULL counts (for comparison):")
print(f"  {REF_CUMFLOW:<45} {ref_cumflow_nulls:>14,}  ({100.0*ref_cumflow_nulls/n_rows:.2f}%)")
print(f"  {REF_FLOWLAGS:<45} {ref_flowlag_nulls:>14,}  ({100.0*ref_flowlag_nulls/n_rows:.2f}%)")

# %%
# --- 7a. NULL counts ---
t1_null_rows = []
for c in TIER1_ALL:
    cur.execute(f"SELECT count(*) FROM {TABLE} WHERE {c} IS NULL;")
    nn = cur.fetchone()[0]
    pct = 100.0 * nn / n_rows

    if c in TIER1_CYCLICAL:
        expected = "0%"
        status = "✓" if nn == 0 else "*** UNEXPECTED NULLs ***"
    elif c in TIER1_CUMFLOW:
        expected = f"~{100.0*ref_cumflow_nulls/n_rows:.1f}% (matches {REF_CUMFLOW})"
        status = "✓" if abs(nn - ref_cumflow_nulls) / max(n_rows, 1) < 0.001 else "check"
    else:
        expected = f"~{100.0*ref_flowlag_nulls/n_rows:.1f}% (matches {REF_FLOWLAGS})"
        status = "✓" if nn <= ref_flowlag_nulls * 1.05 else "check"

    t1_null_rows.append({
        "column": c,
        "null_count": f"{nn:,}",
        "pct": f"{pct:.2f}%",
        "expected": expected,
        "status": status,
    })

pd.DataFrame(t1_null_rows).set_index("column")

# %%
# --- 7b. inf / -inf ---
inf_exprs_t1 = ", ".join(
    f"SUM(CASE WHEN {c} = 'Infinity'::float8 OR {c} = '-Infinity'::float8 "
    f"THEN 1 ELSE 0 END) AS {c}"
    for c in TIER1_ALL
)
cur.execute(f"SELECT {inf_exprs_t1} FROM {TABLE};")
inf_t1 = dict(zip(TIER1_ALL, cur.fetchone()))
inf_t1_df = (
    pd.DataFrame([{"column": c, "inf_count": v or 0} for c, v in inf_t1.items()])
    .query("inf_count > 0")
    .reset_index(drop=True)
)
if inf_t1_df.empty:
    print("inf / -inf check: (none) — all 14 columns clean")
else:
    print("*** inf values found — investigate before training ***")
    inf_t1_df

# %%
# --- 7c. Constant columns ---
const_t1_rows = []
for c in TIER1_ALL:
    cur.execute(f"SELECT min({c}), max({c}), count({c}) FROM {TABLE};")
    mn, mx, nonnull = cur.fetchone()
    if nonnull and mn is not None and mn == mx:
        const_t1_rows.append({"column": c, "constant_value": mn})

if not const_t1_rows:
    print("Constant column check: (none) — all 14 columns have real variance")
else:
    print("*** constant columns found — drop before training ***")
    pd.DataFrame(const_t1_rows)

# %% [markdown]
# ## Summary
# - **Targets**: must be 0 NULLs (check section 1)
# - **XGBoost**: train as-is — handles NaN natively, scale-invariant
# - **Linear / Ridge / Logistic**: wrap in `Pipeline(SimpleImputer → StandardScaler)`
#   fit inside each CV fold; never fit the imputer on the full dataset
# - **Drop before any model**: `rate_of_change_10/20/30min` (100% NULL), any
#   constant columns from section 4
# - **Encode before numeric model**: `season`, `station_role` (section 6)
# - **Do NOT impute**: `nearest_entrance_dist_m` structural NULLs — use a
#   sentinel value (e.g. 800) instead; the signal is already in `is_within_400m`

# %%
conn.close()
print("Audit complete.")

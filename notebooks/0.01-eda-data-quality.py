# %% [markdown]
# # 0.01 — Data Quality & Cleaning (station availability)
#
# Validate raw availability data BEFORE it flows into `training_features`. Bad
# values silently corrupt the training set, so this pass runs FIRST.
#
# Work **one month at a time** (a month fits in RAM; the 334M-row archive does
# not). Develop the checks on a short window, then widen / loop month by month.
#
# Authored as a `# %%` .py file (clean git diffs); export to `.ipynb` with
# outputs for the GitHub showcase via:
#   Command Palette -> "Jupyter: Export Current Python File as Jupyter Notebook"
#
# ---
# ## The checklist this notebook implements (Section A — fix in the dataframe)
#
# **Will CRASH the fit:** NaNs (Linear/Ridge/Logistic throw), inf/-inf, string
# columns fed to numeric models, mixed/object dtypes.
#
# **Will SILENTLY corrupt the model:** outliers / impossible values
# (`bikes > capacity`, negatives), zero-variance columns, duplicate/collinear
# features, stuck sensors, rows with NULL target.
#
# **Data-quality basics:** duplicate `(station_id, fetched_at)`, legit-vs-defect
# nulls, range checks, time gaps, timezone consistency, station_id drift.
#
# (Section B — leakage, CV splitting, scaler placement — is NOT cleanable here;
# it's handled in `train_model.py`. See CLAUDE.md "Data Cleaning & Validation".)

# %%
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Make the `citibike` package importable when this runs from the project root.
sys.path.insert(0, str(Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()))
from citibike import dataset  # noqa: E402

pd.set_option("display.max_columns", 40)
pd.set_option("display.width", 160)

# %% [markdown]
# ## Load one window
#
# Start SMALL (a week) so iteration is fast — `station_status` is polled every
# ~2.5 min, so a full month is tens of millions of rows. Widen once the checks
# are stable, or loop this notebook month by month.

# %%
START = "2026-05-01"
END = "2026-05-08"   # one week to start; widen to a full month when ready
SOURCE_TABLE = "station_status"   # use station_status_pre2021 for historical months

sql = f"""
    SELECT fetched_at, station_id,
           num_bikes_available, num_ebikes_available, num_docks_available,
           num_bikes_disabled, num_docks_disabled,
           is_installed, is_renting, is_returning
    FROM {SOURCE_TABLE}
    WHERE fetched_at >= %(start)s AND fetched_at < %(end)s;
"""
df = dataset.query(sql, params={"start": START, "end": END})
print(f"Loaded {len(df):,} rows from {SOURCE_TABLE}  [{START} -> {END})")
df.head()

# %%
# Merge in capacity (lives in station_information, not station_status) so we can
# range-check bikes vs capacity. station_information is small (~4k rows).
info = dataset.load_station_information()[["station_id", "capacity"]]
df = df.merge(info, on="station_id", how="left")
print(f"Rows missing a capacity match (orphan station_ids): "
      f"{df['capacity'].isna().sum():,}")

# %% [markdown]
# ## 1. Shape, dtypes, memory

# %%
print(df.dtypes)
print(f"\nMemory: {df.memory_usage(deep=True).sum() / 1e6:,.1f} MB")
print(f"Stations: {df['station_id'].nunique():,}")
print(f"Time span: {df['fetched_at'].min()} -> {df['fetched_at'].max()}")

# %% [markdown]
# ## 2. Duplicates — `(station_id, fetched_at)` should be unique

# %%
dupe_mask = df.duplicated(subset=["station_id", "fetched_at"], keep=False)
print(f"Duplicate (station_id, fetched_at) rows: {dupe_mask.sum():,}")
if dupe_mask.any():
    display(df[dupe_mask].sort_values(["station_id", "fetched_at"]).head(10))

# %% [markdown]
# ## 3. Null profile — distinguish *legit* nulls from *defects*
#
# `num_ebikes_available` is legitimately NULL pre-2018 (no e-bikes existed);
# nulls in `num_bikes_available` or `capacity` are defects that break the build.

# %%
null_counts = df.isna().sum()
null_pct = (null_counts / len(df) * 100).round(2)
nulls = pd.DataFrame({"null_count": null_counts, "null_pct": null_pct})
nulls[nulls["null_count"] > 0].sort_values("null_count", ascending=False)

# %% [markdown]
# ## 4. Impossible / out-of-range values (silent model-corruptors)

# %%
checks = {
    "negative bikes": (df["num_bikes_available"] < 0),
    "negative docks": (df["num_docks_available"] < 0),
    "bikes > capacity": (df["num_bikes_available"] > df["capacity"]),
    "capacity <= 0": (df["capacity"] <= 0),
    # rough consistency: bikes + docks shouldn't wildly exceed capacity
    "bikes+docks >> capacity": (
        (df["num_bikes_available"] + df["num_docks_available"]) > (df["capacity"] * 1.5)
    ),
}
range_report = pd.Series({name: int(mask.sum()) for name, mask in checks.items()})
print("Rows failing each range check:")
print(range_report)

# %% [markdown]
# ## 5. Mixed dtypes / object columns that should be numeric or boolean
#
# These crash numeric models. (The Kaggle loader specifically had booleans
# stored as `1.0/0.0/NaN` floats.)

# %%
object_cols = df.select_dtypes(include="object").columns.tolist()
print(f"Object/string columns: {object_cols}")
for c in ["is_installed", "is_renting", "is_returning"]:
    print(f"  {c}: dtype={df[c].dtype}, unique={df[c].dropna().unique()[:5]}")

# %% [markdown]
# ## 6. Time gaps — stretches where ingestion was down
#
# Expected cadence ~2.5 min. Flag per-station gaps far larger than that.

# %%
df_sorted = df.sort_values(["station_id", "fetched_at"])
df_sorted["gap_min"] = (
    df_sorted.groupby("station_id")["fetched_at"].diff().dt.total_seconds() / 60
)
GAP_THRESHOLD_MIN = 15  # >6x the expected 2.5-min cadence
gaps = df_sorted[df_sorted["gap_min"] > GAP_THRESHOLD_MIN]
print(f"Gaps > {GAP_THRESHOLD_MIN} min: {len(gaps):,}")
print(f"Largest gap: {df_sorted['gap_min'].max():,.0f} min")
gaps[["station_id", "fetched_at", "gap_min"]].sort_values("gap_min", ascending=False).head(10)

# %% [markdown]
# ## 7. Stuck sensors — a value frozen for a long time looks like fake availability

# %%
# Stations whose bike count never changes across the whole window = suspicious.
variance_by_station = df.groupby("station_id")["num_bikes_available"].nunique()
frozen = variance_by_station[variance_by_station == 1]
print(f"Stations with a CONSTANT bike count over the window: {len(frozen):,}")
print("(some are genuinely dead/decommissioned docks; inspect before deciding)")
frozen.head(10)

# %% [markdown]
# ## 8. Timezone consistency — wrong tz silently corrupts lag joins

# %%
print(f"fetched_at dtype: {df['fetched_at'].dtype}")
print(f"timezone-aware: {df['fetched_at'].dt.tz is not None}")

# %% [markdown]
# ## Summary
#
# Roll the checks into one verdict so this is glanceable when looping months.

# %%
summary = {
    "rows": len(df),
    "stations": df["station_id"].nunique(),
    "duplicate_rows": int(dupe_mask.sum()),
    "orphan_station_ids (no capacity)": int(df["capacity"].isna().sum()),
    "negative_values": int(checks["negative bikes"].sum() + checks["negative docks"].sum()),
    "bikes_gt_capacity": int(checks["bikes > capacity"].sum()),
    "time_gaps_gt_15min": len(gaps),
    "frozen_stations": len(frozen),
}
pd.Series(summary, name="data_quality_summary").to_frame()

# %% [markdown]
# ## Next: generalize into `clean_month(df)`
#
# Once the rules are decided (drop vs impute vs flag for each issue above), wrap
# them in a function so you can loop month by month. Skeleton:

# %%
def clean_month(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the agreed Section-A cleaning rules to one month of availability.

    TODO — fill in once rules are decided from the EDA above:
      - drop duplicate (station_id, fetched_at)
      - drop / fix negative and bikes>capacity rows
      - decide null policy per column (legit pre-2018 ebike nulls vs defects)
      - coerce is_* columns to clean booleans
      - flag (don't necessarily drop) frozen-sensor stations
    Returns the cleaned frame; cleaning is row-local so no cross-month buffer
    is needed here (that only applies when building lag features).
    """
    out = df.copy()
    out = out.drop_duplicates(subset=["station_id", "fetched_at"])
    # ... add agreed rules here ...
    return out

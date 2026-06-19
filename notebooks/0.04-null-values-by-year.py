# %% [markdown]
# # 0.04 — NULL Values by Year
#
# Breaks down NULL rates per feature column for each training year (2019, 2021, 2026).
# Shows which NULLs are era-specific data gaps vs structural issues that persist
# across all years — informs which years to include in the training window.
#
# Authored as a `# %%` .py file (clean git diffs). Export to `.ipynb` with outputs via:
#   Command Palette → "Jupyter: Export Current Python File as Jupyter Notebook"

# %%
import sys
from pathlib import Path

import pandas as pd
import psycopg2

sys.path.insert(0, str(Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()))
from citibike.config import DB_CONFIG  # noqa: E402
from model_training.build_training_features_pandas import (  # noqa: E402
    INSERT_COLUMNS, INT64_COLUMNS, BOOL_COLUMNS,
)

pd.set_option("display.max_rows", 100)
pd.set_option("display.max_columns", 20)

# %%
TABLE = "training_features"
YEARS = [2019, 2021, 2026]

PK      = ["station_id", "timestamp", "horizon_minutes"]
TARGETS = ["bikes_available_at_horizon", "bike_available_binary"]
TEXT_COLS = ["season", "station_role"]

feature_cols = [c for c in INSERT_COLUMNS if c not in PK and c not in TARGETS]

def q(col):
    return f'"{col}"' if col == "timestamp" else col

conn = psycopg2.connect(**DB_CONFIG)
cur  = conn.cursor()

# Row count per year (single query)
cur.execute(f"""
    SELECT EXTRACT(YEAR FROM "timestamp")::int AS yr, COUNT(*) AS total
    FROM {TABLE}
    WHERE EXTRACT(YEAR FROM "timestamp") = ANY(%(years)s)
    GROUP BY yr ORDER BY yr;
""", {"years": YEARS})
year_totals = {yr: total for yr, total in cur.fetchall()}
print("Row counts per year:")
for yr, total in year_totals.items():
    print(f"  {yr}: {total:,}")

# %% [markdown]
# ## NULL rates per feature, per year
#
# One query per year — each is a single full scan of that year's rows.
# Columns sorted by their 2019 NULL rate (highest first) so the worst offenders
# appear at the top.

# %%
null_exprs = ", ".join(
    f"SUM(CASE WHEN {q(c)} IS NULL THEN 1 ELSE 0 END) AS col_{i}"
    for i, c in enumerate(feature_cols)
)

results = {}
for yr in YEARS:
    cur.execute(f"""
        SELECT {null_exprs}
        FROM {TABLE}
        WHERE EXTRACT(YEAR FROM "timestamp") = %(yr)s;
    """, {"yr": yr})
    row = cur.fetchone()
    total = year_totals.get(yr, 1)
    results[yr] = {
        c: round(100.0 * (row[i] or 0) / total, 1)
        for i, c in enumerate(feature_cols)
    }

null_pct = pd.DataFrame(results, columns=YEARS)
null_pct.index = feature_cols

# Sort by 2019 NULL % descending, drop rows that are 0% across all years
null_pct = null_pct[null_pct.max(axis=1) > 0].sort_values(2019, ascending=False)
null_pct.columns = [f"{yr} (%)" for yr in YEARS]
null_pct

# %% [markdown]
# ## Summary: which NULLs are era-specific vs structural?
#
# - **Same across all years** → structural (data never existed; encode-as-missing)
# - **High in 2019, low in 2026** → legacy era gap (no fix without external data)
# - **High in 2019, gone in 2021/2026** → year-specific data coverage issue

# %%
df = null_pct.copy()
df.columns = [2019, 2021, 2026]

era_specific  = df[(df[2019] > 50) & (df[2026] < 20)].index.tolist()
structural    = df[(df[2019] > 50) & (df[2026] > 50)].index.tolist()
low_all_years = df[df.max(axis=1) < 10].index.tolist()

print("ERA-SPECIFIC NULLs (high in 2019, populated in 2026):")
print("  → caused by data unavailability in the pre-2021 era")
for c in era_specific:
    print(f"  {c:<40} 2019={df.loc[c,2019]:.1f}%  2026={df.loc[c,2026]:.1f}%")

print("\nSTRUCTURAL NULLs (high across all years):")
print("  → data genuinely doesn't exist; encode-as-missing, do not impute")
for c in structural:
    print(f"  {c:<40} 2019={df.loc[c,2019]:.1f}%  2026={df.loc[c,2026]:.1f}%")

print("\nLOW NULLs across all years (< 10%):")
print("  → well-populated; minor imputation only for linear models")
for c in low_all_years:
    print(f"  {c:<40} 2019={df.loc[c,2019]:.1f}%  2021={df.loc[c,2021]:.1f}%  2026={df.loc[c,2026]:.1f}%")

# %%
conn.close()
print("Done.")

"""Shared preprocessing for Phase 3 model training notebooks.

I keep all prep logic here so 2.02, 2.04, 2.05 stay in sync and I don't
accidentally drift the regression and classification tracks apart.

Public API:
    load_training_data(horizon_minutes, years) -> pd.DataFrame
    build_preprocessor(model_type)            -> ColumnTransformer
    FEATURE_COLS                              -> list[str]
    DROP_COLS                                 -> list[str]
    TARGET_REGRESSION                         -> str
    TARGET_CLASSIFICATION                     -> str
    TRAINING_YEARS                            -> list[int]
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from citibike.config import DB_CONFIG  # noqa: E402
from model_training.build_training_features_pandas import (  # noqa: E402
    BOOL_COLUMNS, INSERT_COLUMNS, INT64_COLUMNS,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_REGRESSION = "bikes_available_at_horizon"
TARGET_CLASSIFICATION = "bike_available_binary"
TRAINING_YEARS = [2019, 2021, 2026]

PK = ["station_id", "timestamp", "horizon_minutes"]
TARGETS = [TARGET_REGRESSION, TARGET_CLASSIFICATION]

# 100% NULL in training years — confirmed by audit_training_features.py --training-years-only.
DROP_COLS = [
    "rate_of_change_10min",
    "rate_of_change_20min",
    "rate_of_change_30min",
    "emptying_frequency",
    "capping_frequency",
    "rebalancing_signal",
    "time_since_last_rebalancing",
    "avg_availability_5_nearest_stations",
    "bikes_same_hour_same_weekday_4wk_avg",
]

# station_id stays an identifier, not a feature — one-hot encoding it breaks cold-start
# (new stations get all-zero) and blows up to ~4,200 sparse columns.
_EXCLUDE_FROM_FEATURES = set(PK + TARGETS + DROP_COLS)

FEATURE_COLS = [c for c in INSERT_COLUMNS if c not in _EXCLUDE_FROM_FEATURES]

_TEXT_COLS = ["season", "station_role"]
_BOOL_FEATURE_COLS = [c for c in BOOL_COLUMNS if c in FEATURE_COLS]
_NUMERIC_COLS = [
    c for c in FEATURE_COLS if c not in _TEXT_COLS and c not in _BOOL_FEATURE_COLS
]

# nearest_entrance_dist_m is NULL for stations with no subway entrance within 800m —
# that's structural, not a data gap. Filling with sentinel 800 (the cutoff) tells the
# model "nothing nearby," which is correct. Median imputation would teach the wrong thing.
_SUBWAY_SENTINEL_COL = "nearest_entrance_dist_m"
_SUBWAY_SENTINEL_VAL = 800.0

# Everything except the subway sentinel gets median imputation for linear models.
# The sentinel is pre-filled in load_training_data before the ColumnTransformer sees it.
_IMPUTE_COLS = [c for c in _NUMERIC_COLS if c != _SUBWAY_SENTINEL_COL]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_training_data(horizon_minutes: int, years: list[int] = None) -> pd.DataFrame:
    """Pull one horizon's worth of training data from training_features.

    Drops the 9 all-NULL columns, fills the subway sentinel, and casts booleans
    to int so every caller gets a ready-to-split DataFrame.
    """
    if years is None:
        years = TRAINING_YEARS

    years_sql = ", ".join(str(y) for y in years)
    cols = ", ".join(
        f'"{c}"' if c == "timestamp" else c
        for c in INSERT_COLUMNS
        if c not in DROP_COLS
    )

    sql = f"""
        SELECT {cols}
        FROM training_features
        WHERE horizon_minutes = {horizon_minutes}
          AND EXTRACT(YEAR FROM "timestamp") IN ({years_sql})
        ORDER BY "timestamp";
    """

    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute("SET max_parallel_workers_per_gather = 0")
        df = pd.read_sql(sql, conn)

    # pd.read_sql can return TIMESTAMPTZ as object dtype — force it so downstream merges don't break.
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    df[_SUBWAY_SENTINEL_COL] = df[_SUBWAY_SENTINEL_COL].fillna(_SUBWAY_SENTINEL_VAL)

    # fillna(False) before casting: stations missing from citibike_station_subway_proximity
    # leave is_within_400m NULL — False is the correct fill (not within 400m).
    for col in _BOOL_FEATURE_COLS:
        if col in df.columns:
            df[col] = df[col].astype("boolean").fillna(False).astype("Int64").astype(float)

    return df


def get_X_y(df: pd.DataFrame, target: str):
    """Split a loaded DataFrame into X (features) and y (target)."""
    X = df[FEATURE_COLS].copy()
    y = df[target].copy()
    return X, y


# ---------------------------------------------------------------------------
# Preprocessor
# ---------------------------------------------------------------------------

def build_preprocessor(model_type: str) -> ColumnTransformer:
    """Build a ColumnTransformer for the given model type.

    "linear": impute median -> StandardScaler on numerics; OHE on categoricals; passthrough booleans.
    "lightgbm" / "xgboost": OHE on categoricals only — trees handle NaN and are scale-invariant.
    LightGBM and XGBoost share the same tree path so both take the same feature matrix.

    station_role NULLs get an "unknown" OHE category via handle_unknown="infrequent_if_exist"
    rather than erroring on unseen values at prediction time.
    """
    if model_type not in ("linear", "lightgbm", "xgboost"):
        raise ValueError(
            f"model_type must be 'linear', 'lightgbm', or 'xgboost', got {model_type!r}"
        )

    cat_transformer = OneHotEncoder(
        handle_unknown="infrequent_if_exist",
        sparse_output=False,
    )

    if model_type == "linear":
        num_transformer = Pipeline([
            # keep_empty_features=True: change_ebikes_*/change_classic_* are 100% NULL
            # in 2019 folds (pre-ebike era). Without this, an all-NaN column crashes the
            # scaler. 0 is the right domain fill — no change when ebikes didn't exist.
            ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("scaler", StandardScaler()),
        ])
    else:
        # Trees handle NaN natively — imputing would lose the missingness signal.
        num_transformer = "passthrough"

    return ColumnTransformer(
        transformers=[
            ("num", num_transformer, _IMPUTE_COLS),
            ("cat", cat_transformer, _TEXT_COLS),
            ("bool", "passthrough", _BOOL_FEATURE_COLS),
            # Subway sentinel already filled in load_training_data — just pass through.
            ("subway", "passthrough", [_SUBWAY_SENTINEL_COL]),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )

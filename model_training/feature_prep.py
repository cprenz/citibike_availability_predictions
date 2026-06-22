"""Shared preprocessing for Phase 3 model training notebooks.

Imported by 2.02, 2.03, 2.04, 2.05 so prep logic lives in one place and
the two modeling tracks (regression / classification) never drift apart.

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

# 100% NULL in all training years — zero signal, drop before any model.
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

# station_id is an identifier, never a feature. One-hot encoding it would break
# cold-start generalization (new stations → all-zero) and explode dimensionality.
_EXCLUDE_FROM_FEATURES = set(PK + TARGETS + DROP_COLS)

FEATURE_COLS = [c for c in INSERT_COLUMNS if c not in _EXCLUDE_FROM_FEATURES]

# Column groups for the preprocessor.
_TEXT_COLS = ["season", "station_role"]
_BOOL_FEATURE_COLS = [c for c in BOOL_COLUMNS if c in FEATURE_COLS]
_NUMERIC_COLS = [
    c for c in FEATURE_COLS if c not in _TEXT_COLS and c not in _BOOL_FEATURE_COLS
]

# nearest_entrance_dist_m is structurally NULL for stations with no subway entrance
# within 800m. Fill with sentinel 800 (the search radius) rather than median — median
# would teach the model the opposite of the truth for these stations.
_SUBWAY_SENTINEL_COL = "nearest_entrance_dist_m"
_SUBWAY_SENTINEL_VAL = 800.0

# Numeric cols that get median imputation for linear models (everything except the
# subway sentinel, which is handled separately before the ColumnTransformer).
_IMPUTE_COLS = [c for c in _NUMERIC_COLS if c != _SUBWAY_SENTINEL_COL]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_training_data(horizon_minutes: int, years: list[int] = None) -> pd.DataFrame:
    """Pull one horizon's worth of training data from training_features.

    Returns a DataFrame with all INSERT_COLUMNS minus the DROP_COLS.
    Applies sentinel fill for nearest_entrance_dist_m and casts booleans to int
    so downstream code doesn't have to remember to do it.
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
        df = pd.read_sql(sql, conn)

    # Coerce timestamp so merges don't crash on object vs datetime64 dtype.
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    # Sentinel fill — structural NULL, not a data gap.
    df[_SUBWAY_SENTINEL_COL] = df[_SUBWAY_SENTINEL_COL].fillna(_SUBWAY_SENTINEL_VAL)

    # Booleans → int so sklearn doesn't see object dtype.
    for col in _BOOL_FEATURE_COLS:
        if col in df.columns:
            df[col] = df[col].astype("boolean").astype("Int64").astype(float)

    return df


def get_X_y(df: pd.DataFrame, target: str):
    """Split a loaded DataFrame into feature matrix X and target series y."""
    X = df[FEATURE_COLS].copy()
    y = df[target].copy()
    return X, y


# ---------------------------------------------------------------------------
# Preprocessor
# ---------------------------------------------------------------------------

def build_preprocessor(model_type: str) -> ColumnTransformer:
    """Build a ColumnTransformer for the given model type.

    model_type: "linear" or "xgboost"

    Linear path:  median imputation → StandardScaler on numerics;
                  one-hot on categoricals; pass-through on booleans.
    XGBoost path: one-hot on categoricals only; no imputation or scaling
                  (XGBoost handles NaN natively and is scale-invariant).

    station_role NULLs become "unknown" via handle_unknown="infrequent_if_exist"
    so the model sees an "unknown" category rather than erroring on unseen values.
    """
    if model_type not in ("linear", "xgboost"):
        raise ValueError(f"model_type must be 'linear' or 'xgboost', got {model_type!r}")

    cat_transformer = OneHotEncoder(
        handle_unknown="infrequent_if_exist",
        sparse_output=False,
    )

    if model_type == "linear":
        num_transformer = Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
        ])
    else:
        # XGBoost handles NaN natively — imputation would hide the missingness
        # signal the model can learn from.
        num_transformer = "passthrough"

    return ColumnTransformer(
        transformers=[
            ("num", num_transformer, _IMPUTE_COLS),
            ("cat", cat_transformer, _TEXT_COLS),
            ("bool", "passthrough", _BOOL_FEATURE_COLS),
            # subway sentinel col passes through already filled — no imputation needed.
            ("subway", "passthrough", [_SUBWAY_SENTINEL_COL]),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )

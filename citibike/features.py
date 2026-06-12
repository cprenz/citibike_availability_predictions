"""Feature engineering functions shared across feature-building and inference.

Keeping these here (rather than in notebooks) guarantees training and serving
compute features identically — the usual source of train/serve skew.
"""

import numpy as np
import pandas as pd

US_HOLIDAYS = None  # populate with a holidays calendar when needed


def add_time_features(df: pd.DataFrame, ts_col: str = "timestamp") -> pd.DataFrame:
    """Derive hour_of_day, day_of_week, month, season, is_weekend from a timestamp."""
    ts = pd.to_datetime(df[ts_col])
    df = df.copy()
    df["hour_of_day"] = ts.dt.hour
    df["day_of_week"] = ts.dt.dayofweek
    df["month"] = ts.dt.month
    df["is_weekend"] = (ts.dt.dayofweek >= 5).astype(int)
    df["season"] = (ts.dt.month % 12 // 3).map(
        {0: "winter", 1: "spring", 2: "summer", 3: "fall"}
    )
    return df


def rate_of_change(series: pd.Series, periods: int) -> pd.Series:
    """Difference in availability over `periods` snapshots (drain/fill rate)."""
    return series.diff(periods)


def emptying_frequency(series: pd.Series) -> float:
    """Fraction of observations where the station was empty (0 bikes)."""
    if len(series) == 0:
        return np.nan
    return float((series == 0).mean())


def capping_frequency(bikes: pd.Series, capacity: int) -> float:
    """Fraction of observations where the station was full."""
    if len(bikes) == 0 or not capacity:
        return np.nan
    return float((bikes >= capacity).mean())

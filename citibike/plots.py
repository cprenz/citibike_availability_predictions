"""Reusable plotting helpers for EDA and reporting notebooks."""

import matplotlib.pyplot as plt
import pandas as pd

from citibike.config import FIGURES_DIR


def save_figure(fig, name: str) -> None:
    """Save a figure to reports/figures with consistent dpi."""
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURES_DIR / f"{name}.png", dpi=150, bbox_inches="tight")


def plot_availability_over_time(df: pd.DataFrame, ts_col: str = "fetched_at",
                                value_col: str = "num_bikes_available", title: str = ""):
    """Line plot of availability for a single station over time."""
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(pd.to_datetime(df[ts_col]), df[value_col], linewidth=0.8)
    ax.set_xlabel("Time")
    ax.set_ylabel("Bikes available")
    ax.set_title(title)
    fig.tight_layout()
    return fig, ax

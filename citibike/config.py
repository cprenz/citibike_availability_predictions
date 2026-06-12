"""Central configuration: paths, DB connection settings, project constants.

Loads DB credentials from data_ingestion/.env so notebooks and the package
share the same connection details as the ingestion scripts.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
INTERIM_DATA_DIR = DATA_DIR / "interim"
PROCESSED_DATA_DIR = DATA_DIR / "processed"
EXTERNAL_DATA_DIR = DATA_DIR / "external"
REPORTS_DIR = PROJECT_ROOT / "reports"
FIGURES_DIR = REPORTS_DIR / "figures"
MODELS_DIR = PROJECT_ROOT / "models"

# --- Database ---
# Credentials live in data_ingestion/.env (gitignored). See .env.example.
load_dotenv(PROJECT_ROOT / "data_ingestion" / ".env")

DB_CONFIG = {
    "host": os.getenv("PGHOST", "localhost"),
    "port": os.getenv("PGPORT", "5555"),
    "dbname": os.getenv("PGDATABASE", "citibike"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
}


def db_url() -> str:
    """SQLAlchemy-style connection URL (handy for pandas.read_sql)."""
    c = DB_CONFIG
    return f"postgresql://{c['user']}:{c['password']}@{c['host']}:{c['port']}/{c['dbname']}"


# --- Modeling constants ---
HORIZONS_MINUTES = [10, 60, 180, 360, 720, 1440, 2880]  # 10min .. multi-day
RANDOM_SEED = 42

# Training data excludes the 2022-April 2026 availability gap and 2020 (COVID).
TRAIN_EXCLUDE_YEARS = [2020]

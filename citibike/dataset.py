"""Functions to load data from the Citibike TimescaleDB database.

Thin helpers used by notebooks and modeling code so connection logic lives
in one place. Use psycopg2 for writes, pandas.read_sql for analysis pulls.
"""

import pandas as pd
import psycopg2

from citibike.config import DB_CONFIG, db_url


def get_connection():
    """Open a psycopg2 connection using the shared DB_CONFIG."""
    return psycopg2.connect(**DB_CONFIG)


def query(sql: str, params=None) -> pd.DataFrame:
    """Run a read-only SQL query and return a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(sql, conn, params=params)


def load_station_information() -> pd.DataFrame:
    """All station metadata (id, name, lat/lon, capacity)."""
    return query("SELECT * FROM station_information;")


def load_subway_proximity() -> pd.DataFrame:
    """Per-station subway proximity features."""
    return query("SELECT * FROM citibike_station_subway_proximity;")


def load_station_status(station_id: str, start: str, end: str) -> pd.DataFrame:
    """Availability snapshots for one station over a time window.

    Reads from both the live and pre-2021 tables and concatenates.
    """
    sql = """
        SELECT fetched_at, station_id, num_bikes_available,
               num_ebikes_available, num_docks_available, num_bikes_disabled
        FROM {table}
        WHERE station_id = %(sid)s AND fetched_at >= %(start)s AND fetched_at < %(end)s
        ORDER BY fetched_at;
    """
    params = {"sid": station_id, "start": start, "end": end}
    live = query(sql.format(table="station_status"), params)
    hist = query(sql.format(table="station_status_pre2021"), params)
    return pd.concat([hist, live], ignore_index=True)

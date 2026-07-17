import os
import psycopg2
import pandas as pd
import snowflake.connector
from snowflake.connector.pandas_tools import write_pandas
from datetime import datetime, timezone
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Pushes ride_explorer_profile (built locally by build_ride_explorer_profile.py)
# to Snowflake, table RIDE_EXPLORER_PROFILE. This is what the web app's
# /api/hourly-profile route actually queries — Snowflake is already funded via
# the existing trial, so this adds no new billing risk, unlike the abandoned
# BigQuery path (see sync_station_flow_to_bigquery.py, blocked on the
# citibike dataset's 60-day default table/partition expiration under
# BigQuery's no-billing sandbox mode — DO NOT resume that script without
# explicit go-ahead; see CLAUDE.md Phase 4 Step 5 for the full root-cause note).
#
# TODO — AUTOMATE THIS. Same as build_ride_explorer_profile.py: right now both
# scripts must be run by hand, in order, after new trip data lands. Wire into
# Task Scheduler alongside CitibikeSnowflakeSyncDaily once this is proven stable.
#
# Full overwrite each run (not incremental) — matches sync_hourly_profile() in
# sync_to_snowflake_daily.py. Simpler and safer than diffing rows, and the table
# is small enough (~15-30M rows) for write_pandas to handle in one bulk COPY.


def pg_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"), port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"), user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def sf_conn():
    key_path = os.path.join(os.path.dirname(__file__), "snowflake_key.p8")
    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    private_key_bytes = private_key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"), user=os.getenv("SNOWFLAKE_USER"),
        private_key=private_key_bytes,
        database=os.getenv("SNOWFLAKE_DATABASE", "CITIBIKE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
    )


def bulk_upload(sf, df, table_name, overwrite=False):
    df.columns = [c.upper() for c in df.columns]
    success, nchunks, nrows, _ = write_pandas(
        conn=sf, df=df, table_name=table_name.upper(), overwrite=overwrite,
        quote_identifiers=False, use_logical_type=True, auto_create_table=True,
    )
    return nrows


def sync_ride_explorer_profile(pg, sf):
    df = pd.read_sql("""
        SELECT station_id, year, month, day_of_week, hour_et,
               station_name, lat, lon, capacity, borough,
               total_departures, total_arrivals,
               total_member_trips, total_casual_trips,
               total_ebike_trips, total_classic_trips,
               hours_sampled
        FROM ride_explorer_profile
    """, pg)
    if df.empty:
        print("  ride_explorer_profile: empty — run build_ride_explorer_profile.py first.")
        return 0
    return bulk_upload(sf, df, "ride_explorer_profile", overwrite=True)


def main():
    print(f"Ride Explorer -> Snowflake sync at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    pg = pg_conn()
    sf = sf_conn()
    try:
        nrows = sync_ride_explorer_profile(pg, sf)
        print(f"  ride_explorer_profile: {nrows:,} rows synced to Snowflake.")
    finally:
        pg.close()
        sf.close()


if __name__ == "__main__":
    main()

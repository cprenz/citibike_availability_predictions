import os
import psycopg2
import pandas as pd
import snowflake.connector
from snowflake.connector.pandas_tools import write_pandas
from datetime import datetime, timezone
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
import warnings

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Runs daily at 10:00 PM via Task Scheduler (CitibikeSnowflakeSyncDaily).
# Syncs station_information (full replace), station_daily_ridership and
# station_daily_status (incremental by date), and station_hourly_profile
# (full replace monthly). model_predictions is handled by the hourly script.


def pg_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def sf_conn():
    key_path = os.path.join(os.path.dirname(__file__), "snowflake_key.p8")
    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    private_key_bytes = private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        private_key=private_key_bytes,
        database=os.getenv("SNOWFLAKE_DATABASE", "CITIBIKE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
    )


def bulk_upload(sf, df, table_name, overwrite=False):
    df.columns = [c.upper() for c in df.columns]
    for col in df.select_dtypes(include=["datetimetz", "datetime64[ns, UTC]"]).columns:
        df[col] = pd.to_datetime(df[col], utc=True)
    success, nchunks, nrows, _ = write_pandas(
        conn=sf,
        df=df,
        table_name=table_name.upper(),
        overwrite=overwrite,
        quote_identifiers=False,
        use_logical_type=True,
    )
    return nrows


def sync_station_info(pg, sf):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_sql("""
            SELECT station_id, name, short_name, lat, lon, capacity,
                   region_id, last_updated
            FROM station_information
        """, pg)
    nrows = bulk_upload(sf, df, "station_information", overwrite=True)
    print(f"  station_information: {nrows} rows synced.")


def sync_ridership(pg, sf):
    cur = sf.cursor()
    cur.execute("SELECT MAX(date) FROM station_daily_ridership")
    max_date = cur.fetchone()[0]
    cur.close()

    where = f"WHERE date > '{max_date}'" if max_date else ""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_sql(f"""
            SELECT
                station_id, date, station_name, borough, lat, lon, capacity,
                total_departures, total_arrivals, net_flow,
                ebike_departures, classic_departures, ebike_pct, classic_pct,
                member_trips, casual_trips, member_pct, casual_pct,
                avg_hourly_departures
            FROM station_daily_ridership
            {where}
            ORDER BY date
        """, pg)

    if df.empty:
        print("  station_daily_ridership: already up to date.")
        return

    print(f"  station_daily_ridership: uploading {len(df):,} rows...")
    nrows = bulk_upload(sf, df, "station_daily_ridership")
    print(f"  station_daily_ridership: {nrows:,} rows synced.")


def sync_daily_status(pg, sf):
    cur = sf.cursor()
    cur.execute("SELECT MAX(date) FROM station_daily_status")
    max_date = cur.fetchone()[0]
    cur.close()

    where = f"WHERE date > '{max_date}'" if max_date else ""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_sql(f"""
            SELECT
                station_id, date, station_name, borough, lat, lon, capacity,
                avg_bikes_available, min_bikes_available, max_bikes_available,
                avg_ebikes_available, avg_classic_available,
                avg_docks_available, avg_bikes_disabled,
                avg_fill_ratio, min_fill_ratio, max_fill_ratio,
                hours_sampled
            FROM station_daily_status
            {where}
            ORDER BY date
        """, pg)

    if df.empty:
        print("  station_daily_status: already up to date.")
        return

    print(f"  station_daily_status: uploading {len(df):,} rows...")
    nrows = bulk_upload(sf, df, "station_daily_status")
    print(f"  station_daily_status: {nrows:,} rows synced.")


def sync_hourly_profile(pg, sf):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_sql("""
            SELECT
                station_id, station_name, borough, lat, lon, capacity,
                hour_of_day,
                avg_departures, avg_arrivals, avg_net_flow,
                avg_ebike_departures, avg_classic_departures,
                avg_bikes_available, avg_ebikes_available, avg_fill_ratio
            FROM station_hourly_profile
            ORDER BY station_id, hour_of_day
        """, pg)

    if df.empty:
        print("  station_hourly_profile: empty — run build_station_hourly_profile.py first.")
        return

    print(f"  station_hourly_profile: uploading {len(df):,} rows...")
    nrows = bulk_upload(sf, df, "station_hourly_profile", overwrite=True)
    print(f"  station_hourly_profile: {nrows:,} rows synced.")


def main():
    print(f"Daily sync at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    pg = pg_conn()
    sf = sf_conn()
    try:
        sync_station_info(pg, sf)
        sync_ridership(pg, sf)
        sync_daily_status(pg, sf)
        sync_hourly_profile(pg, sf)
        print("Done.")
    finally:
        pg.close()
        sf.close()


if __name__ == "__main__":
    main()

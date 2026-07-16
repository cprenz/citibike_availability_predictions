import os
import psycopg2
import pandas as pd
import snowflake.connector
from snowflake.connector.pandas_tools import write_pandas
from datetime import datetime, timezone
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Syncs three tables from local PostgreSQL to Snowflake using bulk upload
# (write_pandas stages data via Snowflake internal stage + COPY INTO —
# 10-100x faster than row-by-row executemany):
#   - station_information     : full replace daily (~2,400 rows)
#   - station_daily_ridership : incremental by date (new dates only)
#   - model_predictions       : incremental by predicted_at (new rows only)


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


def sf_max(sf, table, column):
    cur = sf.cursor()
    cur.execute(f"SELECT MAX({column}) FROM {table}")
    result = cur.fetchone()[0]
    cur.close()
    return result


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


# ---------------------------------------------------------------------------
# station_information — full replace (tiny, ~2,400 rows)
# ---------------------------------------------------------------------------

def sync_station_info(pg, sf):
    df = pd.read_sql("""
        SELECT station_id, name, short_name, lat, lon, capacity,
               region_id, last_updated
        FROM station_information
    """, pg)
    nrows = bulk_upload(sf, df, "station_information", overwrite=True)
    print(f"  station_information: {nrows} rows synced.")


# ---------------------------------------------------------------------------
# station_daily_ridership — incremental by date
# ---------------------------------------------------------------------------

def sync_ridership(pg, sf):
    max_date = sf_max(sf, "station_daily_ridership", "date")
    where = f"WHERE date > '{max_date}'" if max_date else ""

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


# ---------------------------------------------------------------------------
# model_predictions — incremental by predicted_at
# ---------------------------------------------------------------------------

def sync_predictions(pg, sf):
    max_predicted_at = sf_max(sf, "model_predictions", "predicted_at")
    where = f"WHERE predicted_at > '{max_predicted_at}'" if max_predicted_at else ""

    df = pd.read_sql(f"""
        SELECT station_id, predicted_at, horizon_minutes, target_time,
               predicted_value_lgbm, predicted_value_linear,
               pi_lower, pi_upper, predicted_prob_logistic, actual_value
        FROM model_predictions
        {where}
        ORDER BY predicted_at
    """, pg)

    if df.empty:
        print("  model_predictions: already up to date.")
        return

    print(f"  model_predictions: uploading {len(df):,} rows...")
    nrows = bulk_upload(sf, df, "model_predictions")
    print(f"  model_predictions: {nrows:,} rows synced.")


# ---------------------------------------------------------------------------

def main():
    print(f"Starting Snowflake sync at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    pg = pg_conn()
    sf = sf_conn()
    try:
        sync_station_info(pg, sf)
        sync_ridership(pg, sf)
        sync_predictions(pg, sf)
        print("Sync complete.")
    finally:
        pg.close()
        sf.close()


if __name__ == "__main__":
    main()

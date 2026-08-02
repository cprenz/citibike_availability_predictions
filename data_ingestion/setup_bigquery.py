"""
One-time BigQuery setup: creates all 9 tables and bulk-loads data.

Data sources:
  - Local PostgreSQL -> model_predictions, station_information,
                        station_daily_ridership, station_daily_status,
                        station_hourly_profile, ride_explorer_profile
  - Snowflake        -> subscribers, unsubscribed
                        (while still accessible; sent_alerts starts fresh)

Run once before switching over:
    python data_ingestion/setup_bigquery.py

Idempotent: tables are created with IF NOT EXISTS; data uses WRITE_TRUNCATE
for full-replace tables and WRITE_APPEND with a MAX-timestamp guard for
incremental tables.
"""

import os
import sys
import warnings
import psycopg2
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

GCP_KEY = os.path.join(os.path.dirname(__file__), "bigquery_key.json")
PROJECT  = "citibike-tableau-501513"
DATASET  = "citibike"

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def bq():
    return bigquery.Client.from_service_account_json(GCP_KEY)


def pg():
    return psycopg2.connect(
        host=os.getenv("PGHOST"), port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"), user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def sf():
    """Snowflake connection — optional, only for subscriber tables."""
    try:
        import snowflake.connector
        from cryptography.hazmat.primitives import serialization
        key_path = os.path.join(os.path.dirname(__file__), "snowflake_key.p8")
        with open(key_path, "rb") as f:
            pk = serialization.load_pem_private_key(f.read(), password=None)
        pk_bytes = pk.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return snowflake.connector.connect(
            account=os.getenv("SNOWFLAKE_ACCOUNT"),
            user=os.getenv("SNOWFLAKE_USER"),
            private_key=pk_bytes,
            database=os.getenv("SNOWFLAKE_DATABASE", "CITIBIKE"),
            schema=os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC"),
            warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        )
    except Exception as e:
        print(f"  WARNING: cannot connect to Snowflake ({e}). Subscriber tables will be empty.")
        return None


def tbl(name):
    return f"`{PROJECT}.{DATASET}.{name}`"


# ---------------------------------------------------------------------------
# Table DDL
# ---------------------------------------------------------------------------

TABLES = {
    "model_predictions": f"""
        CREATE TABLE IF NOT EXISTS {tbl("model_predictions")} (
            station_id                STRING        NOT NULL,
            predicted_at              TIMESTAMP     NOT NULL,
            horizon_minutes           INT64         NOT NULL,
            target_time               TIMESTAMP,
            predicted_value_lgbm      FLOAT64,
            predicted_value_linear    FLOAT64,
            pi_lower                  FLOAT64,
            pi_upper                  FLOAT64,
            predicted_prob_logistic   FLOAT64,
            actual_value              FLOAT64
        )
        PARTITION BY DATE(predicted_at)
        CLUSTER BY station_id
    """,
    "station_information": f"""
        CREATE TABLE IF NOT EXISTS {tbl("station_information")} (
            station_id    STRING  NOT NULL,
            name          STRING,
            short_name    STRING,
            lat           FLOAT64,
            lon           FLOAT64,
            capacity      INT64,
            region_id     STRING,
            last_updated  TIMESTAMP
        )
    """,
    "station_daily_ridership": f"""
        CREATE TABLE IF NOT EXISTS {tbl("station_daily_ridership")} (
            station_id            STRING   NOT NULL,
            date                  DATE     NOT NULL,
            station_name          STRING,
            borough               STRING,
            lat                   FLOAT64,
            lon                   FLOAT64,
            capacity              INT64,
            total_departures      INT64,
            total_arrivals        INT64,
            net_flow              INT64,
            ebike_departures      INT64,
            classic_departures    INT64,
            ebike_pct             FLOAT64,
            classic_pct           FLOAT64,
            member_trips          INT64,
            casual_trips          INT64,
            member_pct            FLOAT64,
            casual_pct            FLOAT64,
            avg_hourly_departures FLOAT64
        )
        PARTITION BY date
        CLUSTER BY station_id
    """,
    "station_daily_status": f"""
        CREATE TABLE IF NOT EXISTS {tbl("station_daily_status")} (
            station_id            STRING   NOT NULL,
            date                  DATE     NOT NULL,
            station_name          STRING,
            borough               STRING,
            lat                   FLOAT64,
            lon                   FLOAT64,
            capacity              INT64,
            avg_bikes_available   FLOAT64,
            min_bikes_available   FLOAT64,
            max_bikes_available   FLOAT64,
            avg_ebikes_available  FLOAT64,
            avg_classic_available FLOAT64,
            avg_docks_available   FLOAT64,
            avg_bikes_disabled    FLOAT64,
            avg_fill_ratio        FLOAT64,
            min_fill_ratio        FLOAT64,
            max_fill_ratio        FLOAT64,
            hours_sampled         INT64
        )
        PARTITION BY date
        CLUSTER BY station_id
    """,
    "station_hourly_profile": f"""
        CREATE TABLE IF NOT EXISTS {tbl("station_hourly_profile")} (
            station_id              STRING  NOT NULL,
            station_name            STRING,
            borough                 STRING,
            lat                     FLOAT64,
            lon                     FLOAT64,
            capacity                INT64,
            hour_of_day             INT64,
            avg_departures          FLOAT64,
            avg_arrivals            FLOAT64,
            avg_net_flow            FLOAT64,
            avg_ebike_departures    FLOAT64,
            avg_classic_departures  FLOAT64,
            avg_bikes_available     FLOAT64,
            avg_ebikes_available    FLOAT64,
            avg_fill_ratio          FLOAT64
        )
    """,
    "ride_explorer_profile": f"""
        CREATE TABLE IF NOT EXISTS {tbl("ride_explorer_profile")} (
            station_id          STRING  NOT NULL,
            year                INT64,
            month               INT64,
            day_of_week         INT64,
            hour_et             INT64,
            station_name        STRING,
            lat                 FLOAT64,
            lon                 FLOAT64,
            capacity            INT64,
            borough             STRING,
            total_departures    INT64,
            total_arrivals      INT64,
            total_member_trips  INT64,
            total_casual_trips  INT64,
            total_ebike_trips   INT64,
            total_classic_trips INT64,
            hours_sampled       INT64
        )
        PARTITION BY RANGE_BUCKET(year, GENERATE_ARRAY(2016, 2030, 1))
        CLUSTER BY station_id
    """,
    "subscribers": f"""
        CREATE TABLE IF NOT EXISTS {tbl("subscribers")} (
            email             STRING,
            phone             STRING,
            station_id        STRING  NOT NULL,
            station_name      STRING,
            target_time       STRING,
            prediction_time   STRING,
            horizon_minutes   INT64   NOT NULL,
            threshold         FLOAT64,
            created_at        TIMESTAMP
        )
    """,
    "sent_alerts": f"""
        CREATE TABLE IF NOT EXISTS {tbl("sent_alerts")} (
            email       STRING  NOT NULL,
            station_id  STRING  NOT NULL,
            alert_date  DATE    NOT NULL,
            sent_at     TIMESTAMP
        )
    """,
    "unsubscribed": f"""
        CREATE TABLE IF NOT EXISTS {tbl("unsubscribed")} (
            email           STRING,
            station_id      STRING,
            station_name    STRING,
            target_time     STRING,
            subscribed_at   TIMESTAMP,
            unsubscribed_at TIMESTAMP
        )
    """,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_tables(client):
    print("Creating tables...")
    for name, ddl in TABLES.items():
        client.query(ddl).result()
        print(f"  {name}: OK")


def upload(client, df, table_name, disposition=bigquery.WriteDisposition.WRITE_APPEND):
    if df.empty:
        print(f"  {table_name}: empty DataFrame — skipped.")
        return 0
    job_config = bigquery.LoadJobConfig(write_disposition=disposition)
    job = client.load_table_from_dataframe(df, f"{PROJECT}.{DATASET}.{table_name}", job_config=job_config)
    job.result()
    return job.output_rows


def read_pg(conn, sql):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.read_sql(sql, conn)


def fix_dates(df, *cols):
    """Convert datetime64 columns to Python date objects so BQ stores as DATE (not TIMESTAMP)."""
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col]).dt.date
    return df


def fix_timestamps(df, *cols):
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)
    return df


# ---------------------------------------------------------------------------
# Load functions — from local PostgreSQL
# ---------------------------------------------------------------------------

def load_model_predictions(client, pg_conn):
    print("  model_predictions: checking existing BQ data...")
    result = client.query(f"SELECT MAX(predicted_at) AS ts FROM {tbl('model_predictions')}").result()
    max_ts = next(iter(result))["ts"]
    where = f"WHERE predicted_at > '{max_ts}'" if max_ts else ""

    df = read_pg(pg_conn, f"""
        SELECT station_id, predicted_at, horizon_minutes, target_time,
               predicted_value_lgbm, predicted_value_linear,
               pi_lower, pi_upper, predicted_prob_logistic, actual_value
        FROM model_predictions
        {where}
        ORDER BY predicted_at
    """)
    if df.empty:
        print("  model_predictions: already up to date.")
        return
    fix_timestamps(df, "predicted_at", "target_time")
    print(f"  model_predictions: uploading {len(df):,} rows...")
    n = upload(client, df, "model_predictions")
    print(f"  model_predictions: {n:,} rows loaded.")


def load_station_info(client, pg_conn):
    df = read_pg(pg_conn, """
        SELECT station_id, name, short_name, lat, lon, capacity, region_id, last_updated
        FROM station_information
    """)
    fix_timestamps(df, "last_updated")
    n = upload(client, df, "station_information", bigquery.WriteDisposition.WRITE_TRUNCATE)
    print(f"  station_information: {n:,} rows loaded.")


def load_daily_ridership(client, pg_conn):
    result = client.query(f"SELECT MAX(date) AS d FROM {tbl('station_daily_ridership')}").result()
    max_date = next(iter(result))["d"]
    where = f"WHERE date > '{max_date}'" if max_date else ""
    df = read_pg(pg_conn, f"""
        SELECT station_id, date, station_name, borough, lat, lon, capacity,
               total_departures, total_arrivals, net_flow,
               ebike_departures, classic_departures, ebike_pct, classic_pct,
               member_trips, casual_trips, member_pct, casual_pct,
               avg_hourly_departures
        FROM station_daily_ridership {where} ORDER BY date
    """)
    if df.empty:
        print("  station_daily_ridership: already up to date.")
        return
    fix_dates(df, "date")
    print(f"  station_daily_ridership: uploading {len(df):,} rows...")
    n = upload(client, df, "station_daily_ridership")
    print(f"  station_daily_ridership: {n:,} rows loaded.")


def load_daily_status(client, pg_conn):
    result = client.query(f"SELECT MAX(date) AS d FROM {tbl('station_daily_status')}").result()
    max_date = next(iter(result))["d"]
    where = f"WHERE date > '{max_date}'" if max_date else ""
    df = read_pg(pg_conn, f"""
        SELECT station_id, date, station_name, borough, lat, lon, capacity,
               avg_bikes_available, min_bikes_available, max_bikes_available,
               avg_ebikes_available, avg_classic_available,
               avg_docks_available, avg_bikes_disabled,
               avg_fill_ratio, min_fill_ratio, max_fill_ratio,
               hours_sampled
        FROM station_daily_status {where} ORDER BY date
    """)
    if df.empty:
        print("  station_daily_status: already up to date.")
        return
    fix_dates(df, "date")
    print(f"  station_daily_status: uploading {len(df):,} rows...")
    n = upload(client, df, "station_daily_status")
    print(f"  station_daily_status: {n:,} rows loaded.")


def load_hourly_profile(client, pg_conn):
    df = read_pg(pg_conn, """
        SELECT station_id, station_name, borough, lat, lon, capacity, hour_of_day,
               avg_departures, avg_arrivals, avg_net_flow,
               avg_ebike_departures, avg_classic_departures,
               avg_bikes_available, avg_ebikes_available, avg_fill_ratio
        FROM station_hourly_profile ORDER BY station_id, hour_of_day
    """)
    if df.empty:
        print("  station_hourly_profile: empty — skipped.")
        return
    n = upload(client, df, "station_hourly_profile", bigquery.WriteDisposition.WRITE_TRUNCATE)
    print(f"  station_hourly_profile: {n:,} rows loaded.")


def load_ride_explorer(client, pg_conn):
    df = read_pg(pg_conn, """
        SELECT station_id, year, month, day_of_week, hour_et,
               station_name, lat, lon, capacity, borough,
               total_departures, total_arrivals,
               total_member_trips, total_casual_trips,
               total_ebike_trips, total_classic_trips,
               hours_sampled
        FROM ride_explorer_profile
    """)
    if df.empty:
        print("  ride_explorer_profile: empty — run build_ride_explorer_profile.py first.")
        return
    print(f"  ride_explorer_profile: uploading {len(df):,} rows (this takes a few minutes)...")
    n = upload(client, df, "ride_explorer_profile", bigquery.WriteDisposition.WRITE_TRUNCATE)
    print(f"  ride_explorer_profile: {n:,} rows loaded.")


# ---------------------------------------------------------------------------
# Load from Snowflake — subscribers + unsubscribed only
# ---------------------------------------------------------------------------

def load_from_snowflake(client, sf_conn):
    if sf_conn is None:
        print("  Skipping Snowflake tables (no connection).")
        return

    # subscribers — pass explicit schema so load_table_from_dataframe cannot
    # corrupt PHONE (all-null -> float64 in pandas) or CREATED_AT types.
    SUBSCRIBERS_SCHEMA = [
        bigquery.SchemaField("email",           "STRING"),
        bigquery.SchemaField("phone",           "STRING"),
        bigquery.SchemaField("station_id",      "STRING"),
        bigquery.SchemaField("station_name",    "STRING"),
        bigquery.SchemaField("target_time",     "STRING"),
        bigquery.SchemaField("prediction_time", "STRING"),
        bigquery.SchemaField("horizon_minutes", "INT64"),
        bigquery.SchemaField("threshold",       "FLOAT64"),
        bigquery.SchemaField("created_at",      "TIMESTAMP"),
    ]
    try:
        df = pd.read_sql("""
            SELECT email, phone, station_id, station_name, target_time,
                   prediction_time, horizon_minutes, threshold, created_at
            FROM subscribers
        """, sf_conn)
        if not df.empty:
            fix_timestamps(df, "created_at")
            df["phone"] = df["phone"].astype(object).where(df["phone"].notna(), None)
            job_config = bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                schema=SUBSCRIBERS_SCHEMA,
            )
            job = client.load_table_from_dataframe(
                df, f"{PROJECT}.{DATASET}.subscribers", job_config=job_config
            )
            job.result()
            n = job.output_rows
            print(f"  subscribers: {n:,} rows loaded from Snowflake.")
        else:
            print("  subscribers: none in Snowflake.")
    except Exception as e:
        print(f"  subscribers: failed ({e}).")

    # unsubscribed
    try:
        df = pd.read_sql("""
            SELECT email, station_id, station_name, target_time,
                   subscribed_at, unsubscribed_at
            FROM unsubscribed
        """, sf_conn)
        if not df.empty:
            fix_timestamps(df, "subscribed_at", "unsubscribed_at")
            n = upload(client, df, "unsubscribed", bigquery.WriteDisposition.WRITE_TRUNCATE)
            print(f"  unsubscribed: {n:,} rows loaded from Snowflake.")
        else:
            print("  unsubscribed: none in Snowflake.")
    except Exception as e:
        print(f"  unsubscribed: failed ({e}).")

    # sent_alerts starts fresh — old Snowflake rows use subscriber_id key
    # which doesn't map cleanly to (email, station_id). At portfolio scale,
    # a subscriber might get one duplicate email on the cutover day. Acceptable.
    print("  sent_alerts: starting fresh (new schema uses email+station_id key).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"BigQuery setup at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Project: {PROJECT}  Dataset: {DATASET}\n")

    client = bq()
    pg_conn = pg()
    sf_conn = sf()

    try:
        create_tables(client)
        print()

        print("Loading from local PostgreSQL...")
        load_model_predictions(client, pg_conn)
        load_station_info(client, pg_conn)
        load_daily_ridership(client, pg_conn)
        load_daily_status(client, pg_conn)
        load_hourly_profile(client, pg_conn)
        load_ride_explorer(client, pg_conn)
        print()

        print("Loading subscriber tables from Snowflake...")
        load_from_snowflake(client, sf_conn)

        print("\nSetup complete.")
    finally:
        pg_conn.close()
        if sf_conn:
            sf_conn.close()


if __name__ == "__main__":
    main()

"""
Pulls the subscribers table from Snowflake and upserts into local PostgreSQL.
Run daily as a backup — keeps a local copy of all signups even if Snowflake
trial expires or the account is unavailable.

Add to Task Scheduler or run manually:
    python data_ingestion/sync_subscribers_from_snowflake.py
"""
import os
import psycopg2
import pandas as pd
import snowflake.connector
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


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


def pg_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def main():
    print("Pulling subscribers from Snowflake...")
    sf = sf_conn()
    cur = sf.cursor()
    cur.execute("""
        SELECT id, email, phone, station_id, horizon_minutes, threshold, created_at
        FROM subscribers
        ORDER BY id
    """)
    rows = cur.fetchall()
    cur.close()
    sf.close()

    if not rows:
        print("No subscribers in Snowflake yet.")
        return

    print(f"  {len(rows)} rows fetched from Snowflake.")

    pg = pg_conn()
    pg_cur = pg.cursor()

    # Ensure the local table exists (matches the PostgreSQL schema already deployed).
    # The Snowflake id is stored as snowflake_id so we don't fight local SERIAL.
    pg_cur.execute("""
        CREATE TABLE IF NOT EXISTS subscribers_snowflake_backup (
            snowflake_id    INTEGER         PRIMARY KEY,
            email           VARCHAR(255),
            phone           VARCHAR(50),
            station_id      VARCHAR(255)    NOT NULL,
            horizon_minutes INTEGER         NOT NULL,
            threshold       INTEGER,
            created_at      TIMESTAMPTZ
        )
    """)

    upserted = 0
    for row in rows:
        sf_id, email, phone, station_id, horizon_minutes, threshold, created_at = row
        pg_cur.execute("""
            INSERT INTO subscribers_snowflake_backup
                (snowflake_id, email, phone, station_id, horizon_minutes, threshold, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (snowflake_id) DO UPDATE SET
                email           = EXCLUDED.email,
                phone           = EXCLUDED.phone,
                station_id      = EXCLUDED.station_id,
                horizon_minutes = EXCLUDED.horizon_minutes,
                threshold       = EXCLUDED.threshold,
                created_at      = EXCLUDED.created_at
        """, (sf_id, email, phone, station_id, horizon_minutes, threshold, created_at))
        upserted += 1

    pg.commit()
    pg_cur.close()
    pg.close()
    print(f"  {upserted} rows upserted into local subscribers_snowflake_backup.")


if __name__ == "__main__":
    main()

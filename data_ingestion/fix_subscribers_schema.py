"""
One-time fix: recreate the BigQuery subscribers table with the correct schema.

Root cause: setup_bigquery.py used load_table_from_dataframe with WRITE_TRUNCATE
to populate subscribers from Snowflake. The DataFrame had phone as all-null float64
and created_at as int64 (nanoseconds — Snowflake stored it as integer). BigQuery
inferred the schema from the DataFrame, overwriting the correct DDL with:
  PHONE     INT64   (should be STRING)
  CREATED_AT INT64  (should be TIMESTAMP)

This caused:
  - created_at displaying as a raw large integer in the BigQuery console
  - new signups failing to write rows (type mismatch on INSERT)
  - no alert emails delivered (subscribers not in BQ, or NULL times)

Fix: read all rows, convert created_at (nanoseconds -> TIMESTAMP), drop table,
recreate with correct DDL, upload rows back with explicit schema.
"""

import os
import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

GCP_KEY = os.path.join(os.path.dirname(__file__), "bigquery_key.json")
PROJECT = "citibike-tableau-501513"
DATASET = "citibike"


CORRECT_SCHEMA = [
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


def main():
    client    = bigquery.Client.from_service_account_json(GCP_KEY)
    table_id  = f"{PROJECT}.{DATASET}.subscribers"
    backtick  = f"`{table_id}`"

    # ------------------------------------------------------------------
    # Step 1 — read existing rows
    # ------------------------------------------------------------------
    print("Step 1: reading existing subscribers...")
    df = client.query(f"SELECT * FROM {backtick}").result().to_dataframe()
    print(f"  {len(df)} rows found.")
    if not df.empty:
        print(f"  Columns/dtypes: {dict(df.dtypes)}")

    # BigQuery returns column names in uppercase — lowercase them so the
    # schema field names and conversion logic below can use lowercase keys.
    df.columns = [c.lower() for c in df.columns]

    # ------------------------------------------------------------------
    # Step 2 — convert created_at nanoseconds -> UTC Timestamp
    # ------------------------------------------------------------------
    if "created_at" in df.columns and not df.empty:
        print("Step 2: converting created_at from INT64 nanoseconds to TIMESTAMP...")
        # The stored values are Unix nanoseconds (e.g. 1785127773713000000).
        # pandas to_datetime with unit='ns' handles this correctly.
        df["created_at"] = pd.to_datetime(df["created_at"], unit="ns", utc=True, errors="coerce")
        non_null = df["created_at"].notna().sum()
        print(f"  Converted {non_null} non-null timestamps.")
        if non_null > 0:
            print(f"  Sample: {df['created_at'].dropna().iloc[0]}")

    # Convert phone: was INT64 null -> ensure it is Python None so BQ treats it as STRING null
    if "phone" in df.columns and not df.empty:
        df["phone"] = df["phone"].astype(object).where(df["phone"].notna(), None)

    # ------------------------------------------------------------------
    # Step 3 — drop the corrupted table
    # ------------------------------------------------------------------
    print("Step 3: dropping the corrupted table...")
    client.query(f"DROP TABLE IF EXISTS {backtick}").result()
    print("  Dropped.")

    # ------------------------------------------------------------------
    # Step 4 — recreate with correct schema
    # ------------------------------------------------------------------
    print("Step 4: recreating with correct schema...")
    ddl = f"""
        CREATE TABLE {backtick} (
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
    """
    client.query(ddl).result()
    print("  Created.")

    # ------------------------------------------------------------------
    # Step 5 — reload the rows with an explicit schema so BQ uses the
    #          correct types (not inferred from the DataFrame)
    # ------------------------------------------------------------------
    if df.empty:
        print("No rows to restore — done.")
        return

    print(f"Step 5: uploading {len(df)} rows back...")
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        schema=CORRECT_SCHEMA,
    )
    job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()
    print(f"  {job.output_rows} rows restored.")

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------
    result = client.query(f"SELECT COUNT(*) AS cnt FROM {backtick}").result()
    count = next(iter(result))["cnt"]
    print(f"\nVerification: {count} rows in table.")

    # Show a sample of converted timestamps
    sample = client.query(
        f"SELECT email, created_at FROM {backtick} LIMIT 3"
    ).result().to_dataframe()
    print("Sample created_at values after fix:")
    print(sample.to_string(index=False))

    print("\nSchema fix complete. Test a new signup on bikepredict.fyi to verify.")


if __name__ == "__main__":
    main()

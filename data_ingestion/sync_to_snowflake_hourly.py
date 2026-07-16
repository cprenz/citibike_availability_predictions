import os
import psycopg2
import pandas as pd
import snowflake.connector
from snowflake.connector.pandas_tools import write_pandas
from datetime import datetime, timezone
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Runs hourly at :10 via Task Scheduler (CitibikeSnowflakeSyncHourly).
# Syncs only model_predictions — the table that grows every hour.
# Incremental: only pushes rows newer than what Snowflake already has.


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


def bulk_upload(sf, df, table_name):
    df.columns = [c.upper() for c in df.columns]
    for col in df.select_dtypes(include=["datetimetz", "datetime64[ns, UTC]"]).columns:
        df[col] = pd.to_datetime(df[col], utc=True)
    success, nchunks, nrows, _ = write_pandas(
        conn=sf,
        df=df,
        table_name=table_name.upper(),
        quote_identifiers=False,
        use_logical_type=True,
    )
    return nrows


def sync_predictions(pg, sf):
    cur = sf.cursor()
    cur.execute("SELECT MAX(predicted_at) FROM model_predictions")
    max_predicted_at = cur.fetchone()[0]
    cur.close()

    where = f"WHERE predicted_at > '{max_predicted_at}'" if max_predicted_at else ""

    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
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


def main():
    print(f"Hourly sync at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    pg = pg_conn()
    sf = sf_conn()
    try:
        sync_predictions(pg, sf)
        print("Done.")
    finally:
        pg.close()
        sf.close()


if __name__ == "__main__":
    main()

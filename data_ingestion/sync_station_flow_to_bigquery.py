import os
import psycopg2
import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account
from datetime import datetime, timezone, date
from dotenv import load_dotenv
import warnings

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Runs once for the initial historical load (all months), then daily after that
# via Task Scheduler (CitibikeBigQuerySync) to pick up new live months.
# Reads station_hourly_flow from PostgreSQL, joins borough + station lat/lon,
# pre-computes ET time fields, and appends incrementally to BigQuery.

PROJECT_ID = "citibike-tableau-501513"
DATASET    = "citibike"
TABLE      = "station_hourly_flow"
FULL_TABLE = f"{PROJECT_ID}.{DATASET}.{TABLE}"

KEY_FILE = os.path.join(
    os.path.dirname(__file__),
    os.getenv("BIGQUERY_KEY_FILE", "bigquery_key.json"),
)


def pg_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def bq_client():
    creds = service_account.Credentials.from_service_account_file(
        KEY_FILE,
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    return bigquery.Client(project=PROJECT_ID, credentials=creds)


SCHEMA = [
    bigquery.SchemaField("station_id",    "STRING",  mode="REQUIRED"),
    bigquery.SchemaField("trip_month",    "DATE",    mode="REQUIRED"),  # partition key
    bigquery.SchemaField("year",          "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("month",         "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("day_of_week",   "INTEGER", mode="REQUIRED"),  # 0=Sun … 6=Sat
    bigquery.SchemaField("hour_et",       "INTEGER", mode="REQUIRED"),  # 0-23 Eastern Time
    bigquery.SchemaField("departures",    "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("arrivals",      "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("member_trips",  "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("casual_trips",  "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("ebike_trips",   "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("classic_trips", "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("station_name",  "STRING",  mode="NULLABLE"),
    bigquery.SchemaField("lat",           "FLOAT",   mode="NULLABLE"),
    bigquery.SchemaField("lon",           "FLOAT",   mode="NULLABLE"),
    bigquery.SchemaField("capacity",      "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("borough",       "STRING",  mode="NULLABLE"),
]


def create_table_if_not_exists(bq):
    table_ref = bigquery.Table(FULL_TABLE, schema=SCHEMA)
    table_ref.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.MONTH,
        field="trip_month",
    )
    table_ref.clustering_fields = ["station_id", "year"]
    bq.create_table(table_ref, exists_ok=True)
    print(f"  Table {FULL_TABLE} ready.")


def get_months_in_bq(bq):
    # Return the SET of months already loaded, not just the max. Resuming from
    # MAX(trip_month) can't backfill a gap below the max: if only the latest month
    # is present, every earlier month gets silently skipped. Comparing full sets
    # loads any month PostgreSQL has that BigQuery is missing, wherever the gap is.
    try:
        rows = list(bq.query(
            f"SELECT DISTINCT trip_month FROM `{FULL_TABLE}`"
        ).result())
        return {row[0] for row in rows}  # set of dates
    except Exception:
        return set()


def get_months_in_pg(pg):
    with pg.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT
                DATE_TRUNC('month', hour AT TIME ZONE 'America/New_York')::date AS m
            FROM station_hourly_flow
            ORDER BY 1
        """)
        return [row[0] for row in cur.fetchall()]


def pull_month(pg, month_start: date) -> pd.DataFrame:
    # Pull one calendar month (ET) at a time to keep memory manageable.
    # Joins station lat/lon via UUID match first, short_name fallback for pre-2021 rows.
    # Timezone conversion happens here so BigQuery stores plain ET integers.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_sql("""
            SELECT
                f.station_id,
                DATE_TRUNC('month', f.hour AT TIME ZONE 'America/New_York')::date
                    AS trip_month,
                EXTRACT(YEAR  FROM f.hour AT TIME ZONE 'America/New_York')::int AS year,
                EXTRACT(MONTH FROM f.hour AT TIME ZONE 'America/New_York')::int AS month,
                EXTRACT(DOW   FROM f.hour AT TIME ZONE 'America/New_York')::int AS day_of_week,
                EXTRACT(HOUR  FROM f.hour AT TIME ZONE 'America/New_York')::int AS hour_et,
                f.departures,
                f.arrivals,
                f.member_trips,
                f.casual_trips,
                f.ebike_trips,
                f.classic_trips,
                COALESCE(si1.name,     si2.name)     AS station_name,
                COALESCE(si1.lat,      si2.lat)      AS lat,
                COALESCE(si1.lon,      si2.lon)      AS lon,
                COALESCE(si1.capacity, si2.capacity) AS capacity,
                sb.borough
            FROM station_hourly_flow f
            LEFT JOIN station_information si1
                ON si1.station_id = f.station_id
            LEFT JOIN station_information si2
                ON si2.short_name  = f.station_id
            LEFT JOIN station_borough sb
                ON sb.station_id = COALESCE(si1.station_id, si2.station_id)
            WHERE DATE_TRUNC('month', f.hour AT TIME ZONE 'America/New_York')::date
                  = %(month_start)s
        """, pg, params={"month_start": month_start})
    return df


def upload_month(bq, df: pd.DataFrame):
    if df.empty:
        return 0
    # trip_month must be a Python date for BigQuery DATE partition field
    df["trip_month"] = pd.to_datetime(df["trip_month"]).dt.date
    job_config = bigquery.LoadJobConfig(
        schema=SCHEMA,
        write_disposition="WRITE_APPEND",
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.MONTH,
            field="trip_month",
        ),
        clustering_fields=["station_id", "year"],
    )
    job = bq.load_table_from_dataframe(df, FULL_TABLE, job_config=job_config)
    job.result()
    return len(df)


def main():
    print(f"BigQuery sync at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    pg  = pg_conn()
    bq  = bq_client()

    create_table_if_not_exists(bq)

    loaded = get_months_in_bq(bq)
    print(f"  Months already in BigQuery: {len(loaded)}")

    months = [m for m in get_months_in_pg(pg) if m not in loaded]

    if not months:
        print("  Already up to date.")
        pg.close()
        return

    print(f"  Months to sync: {len(months)}")
    total = 0
    for month in months:
        print(f"  {month} ...", end=" ", flush=True)
        df = pull_month(pg, month)
        n  = upload_month(bq, df)
        total += n
        print(f"{n:,} rows")

    print(f"Done. {total:,} total rows synced.")
    pg.close()


if __name__ == "__main__":
    main()

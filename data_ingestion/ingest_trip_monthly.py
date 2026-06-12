import io
import os
import sys
import zipfile
import requests
import pandas as pd
import psycopg2
from datetime import date
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# Runs monthly via Windows Task Scheduler (CitibikeTripMonthly, 6th of each month).
# Downloads the previous month's trip zip from Citibike S3, aggregates into
# station_hourly_flow, then recomputes station_demand_profile and
# station_trip_features.
#
# Usage (manual):
#   python data_ingestion/ingest_trip_monthly.py
#
# The script always targets the previous calendar month.

BASE_URL         = "https://s3.amazonaws.com/tripdata"
DOWNLOAD_TIMEOUT = 300


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(
        host=os.getenv('PGHOST'),
        port=int(os.getenv('PGPORT')),
        dbname=os.getenv('PGDATABASE'),
        user=os.getenv('PGUSER'),
        password=os.getenv('PGPASSWORD'),
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_month(y, m):
    """Download zip for given year/month from S3. Returns bytes or None."""
    ym = f"{y:04d}{m:02d}"
    candidates = (
        [f"{ym}-citibike-tripdata.zip", f"{ym}-citibike-tripdata.csv.zip"]
        if y >= 2021 else
        [f"{ym}-citibike-tripdata.csv.zip", f"{ym}-citibike-tripdata.zip"]
    )
    for filename in candidates:
        url = f"{BASE_URL}/{filename}"
        try:
            r = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
            if r.status_code == 200:
                data = b"".join(r.iter_content(chunk_size=1024 * 1024))
                print(f"Downloaded {filename} ({len(data) / 1e6:.1f} MB)")
                return data
            elif r.status_code == 404:
                continue
            else:
                print(f"HTTP {r.status_code} for {url}")
        except Exception as e:
            print(f"Error fetching {url}: {e}")
    return None


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------

def normalize(df):
    df.columns = df.columns.str.strip()

    if 'ride_id' in df.columns:
        # New schema (2021+) — column renamed from member_or_casual to member_casual in 2024
        df = df.rename(columns={'member_or_casual': 'user_type', 'member_casual': 'user_type'})
        df['is_member'] = df['user_type'] == 'member'
        rideable = df.get('rideable_type', pd.Series('classic_bike', index=df.index))
        df['is_ebike'] = rideable == 'electric_bike'
    else:
        # Old schema (pre-2021)
        df = df.rename(columns={
            'starttime':        'started_at',
            'stoptime':         'ended_at',
            'start station id': 'start_station_id',
            'end station id':   'end_station_id',
            'usertype':         'user_type',
        })
        df['is_member'] = df['user_type'] == 'Subscriber'
        df['is_ebike']  = False

    df['is_casual']  = ~df['is_member']
    df['is_classic'] = ~df['is_ebike']

    df['started_at'] = pd.to_datetime(df['started_at'], errors='coerce')
    df['ended_at']   = pd.to_datetime(df['ended_at'],   errors='coerce')

    df = df.dropna(subset=['started_at', 'ended_at', 'start_station_id', 'end_station_id'])

    df['start_station_id'] = df['start_station_id'].astype(str).str.strip()
    df['end_station_id']   = df['end_station_id'].astype(str).str.strip()
    df = df[(df['start_station_id'] != '') & (df['start_station_id'] != 'nan')]
    df = df[(df['end_station_id']   != '') & (df['end_station_id']   != 'nan')]

    return df


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def aggregate(df):
    if df.empty:
        return pd.DataFrame()

    dep_hour = df['started_at'].dt.floor('h')
    arr_hour = df['ended_at'].dt.floor('h')

    dep = (
        df.assign(hour=dep_hour)
        .groupby(['start_station_id', 'hour'])
        .agg(
            departures=('is_member',    'count'),
            member_trips=('is_member',  'sum'),
            casual_trips=('is_casual',  'sum'),
            ebike_trips=('is_ebike',    'sum'),
            classic_trips=('is_classic','sum'),
        )
        .reset_index()
        .rename(columns={'start_station_id': 'station_id'})
    )

    arr = (
        df.assign(hour=arr_hour)
        .groupby(['end_station_id', 'hour'])
        .agg(arrivals=('is_member', 'count'))
        .reset_index()
        .rename(columns={'end_station_id': 'station_id'})
    )

    merged = dep.merge(arr, on=['station_id', 'hour'], how='outer').fillna(0)

    for col in ['departures', 'arrivals', 'member_trips',
                'casual_trips', 'ebike_trips', 'classic_trips']:
        merged[col] = merged[col].astype(int)

    return merged


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_hourly_flow(conn, df):
    if df.empty:
        return 0

    cols = ['station_id', 'hour', 'departures', 'arrivals',
            'member_trips', 'casual_trips', 'ebike_trips', 'classic_trips']
    rows = [tuple(r) for r in df[cols].itertuples(index=False, name=None)]

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO station_hourly_flow
                (station_id, hour, departures, arrivals,
                 member_trips, casual_trips, ebike_trips, classic_trips)
            VALUES %s
            ON CONFLICT (station_id, hour) DO UPDATE SET
                departures    = EXCLUDED.departures,
                arrivals      = EXCLUDED.arrivals,
                member_trips  = EXCLUDED.member_trips,
                casual_trips  = EXCLUDED.casual_trips,
                ebike_trips   = EXCLUDED.ebike_trips,
                classic_trips = EXCLUDED.classic_trips
        """, rows)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Recompute derived tables
# ---------------------------------------------------------------------------

def recompute_demand_profile(conn):
    print("Recomputing station_demand_profile...")
    with conn.cursor() as cur:
        cur.execute("TRUNCATE station_demand_profile")
        cur.execute("""
            INSERT INTO station_demand_profile
                (station_id, hour_of_day, day_of_week,
                 avg_departures, avg_arrivals, avg_net_flow)
            SELECT
                station_id,
                EXTRACT(HOUR FROM hour)::SMALLINT,
                EXTRACT(DOW  FROM hour)::SMALLINT,
                AVG(departures),
                AVG(arrivals),
                AVG(departures - arrivals)
            FROM station_hourly_flow
            GROUP BY station_id,
                     EXTRACT(HOUR FROM hour),
                     EXTRACT(DOW  FROM hour)
        """)
    conn.commit()
    print("  Done.")


def recompute_trip_features(conn):
    print("Recomputing station_trip_features...")
    with conn.cursor() as cur:
        cur.execute("TRUNCATE station_trip_features")
        cur.execute("""
            INSERT INTO station_trip_features
                (station_id, member_ratio, ebike_ratio,
                 avg_daily_departures, avg_daily_arrivals,
                 station_role, computed_at)
            SELECT
                station_id,
                CASE WHEN SUM(departures) > 0
                     THEN SUM(member_trips)::FLOAT / SUM(departures) END,
                CASE WHEN SUM(departures) > 0
                     THEN SUM(ebike_trips)::FLOAT  / SUM(departures) END,
                SUM(departures)::FLOAT
                    / NULLIF(COUNT(DISTINCT DATE_TRUNC('day', hour)), 0),
                SUM(arrivals)::FLOAT
                    / NULLIF(COUNT(DISTINCT DATE_TRUNC('day', hour)), 0),
                CASE
                    WHEN AVG(departures) > AVG(arrivals) * 1.2 THEN 'source'
                    WHEN AVG(arrivals)   > AVG(departures) * 1.2 THEN 'sink'
                    ELSE 'balanced'
                END,
                NOW()
            FROM station_hourly_flow
            GROUP BY station_id
        """)
    conn.commit()
    print("  Done.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def already_loaded(conn, y, m):
    """Return True if station_hourly_flow already has rows for this month."""
    start = f"{y}-{m:02d}-01"
    end   = f"{y+1}-01-01" if m == 12 else f"{y}-{m+1:02d}-01"
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM station_hourly_flow
            WHERE hour >= %s AND hour < %s
            LIMIT 1
        """, (start, end))
        return cur.fetchone() is not None


def main():
    today = date.today()
    if today.month == 1:
        y, m = today.year - 1, 12
    else:
        y, m = today.year, today.month - 1

    print(f"Citibike monthly trip ingest — target month: {y}-{m:02d}")

    conn = get_conn()
    if already_loaded(conn, y, m):
        print(f"{y}-{m:02d} already loaded — nothing to do.")
        conn.close()
        return
    conn.close()

    data = download_month(y, m)
    if data is None:
        print(f"ERROR: {y}-{m:02d} zip not found on S3. Exiting.")
        sys.exit(1)

    conn = get_conn()
    total = 0

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        csvs = sorted(
            n for n in zf.namelist()
            if n.endswith('.csv') and not os.path.basename(n).startswith('.')
        )
        print(f"Found {len(csvs)} CSV(s) in zip.")
        for csv_name in csvs:
            print(f"  Processing {os.path.basename(csv_name)}...")
            with zf.open(csv_name) as f:
                df = pd.read_csv(f, low_memory=False)
            df  = normalize(df)
            agg = aggregate(df)
            if agg.empty:
                print(f"  No valid rows.")
                continue
            inserted = upsert_hourly_flow(conn, agg)
            total += inserted
            print(f"  Upserted {inserted:,} station-hour rows.")

    print(f"\nTotal upserted: {total:,} station-hour rows.")

    recompute_demand_profile(conn)
    recompute_trip_features(conn)

    conn.close()
    print("Done.")


if __name__ == '__main__':
    main()

import io
import os
import sys
import zipfile
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'data_ingestion', '.env'))

# One-time backfill: reads Citibike monthly trip CSVs from a local folder of zips,
# aggregates into station_hourly_flow, then recomputes station_demand_profile
# and station_trip_features.
#
# Usage:
#   python data_historical/fetch_trip_csvs_historical.py [path/to/trip_zips]
#
# Defaults to data_historical/trip_zips if no path given.
# Handles both annual zips (2019-citibike-tripdata.zip containing 12 monthly CSVs)
# and individual monthly zips (202401-citibike-tripdata.zip).
# Skips 2020 (COVID anomaly).
# Checkpoints per CSV — re-run same command to resume if interrupted.
# Aggregates on load — raw trips are never stored.

SKIP_YEARS      = {2020}
CHECKPOINT_FILE = os.path.join(os.path.dirname(__file__), 'trip_load_checkpoint.txt')
DEFAULT_FOLDER  = os.path.join(os.path.dirname(__file__), 'trip_zips')


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
# Checkpoint
# ---------------------------------------------------------------------------

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return set(f.read().splitlines())
    return set()


def save_checkpoint(key):
    with open(CHECKPOINT_FILE, 'a') as f:
        f.write(key + '\n')


# ---------------------------------------------------------------------------
# Scan folder
# ---------------------------------------------------------------------------

def scan_zips(folder):
    """Yield (zip_path, csv_name) for every CSV in every zip in the folder.

    Annual zips (e.g. 2019-citibike-tripdata.zip) yield one entry per
    monthly CSV inside. Monthly zips yield one entry total.
    """
    for fname in sorted(os.listdir(folder)):
        if not fname.lower().endswith('.zip'):
            continue
        zip_path = os.path.join(folder, fname)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                csvs = sorted(
                    n for n in zf.namelist()
                    if n.endswith('.csv') and not os.path.basename(n).startswith('.')
                )
                if not csvs:
                    print(f"Warning: no CSVs found in {fname}")
                    continue
                for csv_name in csvs:
                    yield zip_path, csv_name
        except Exception as e:
            print(f"Warning: could not read {fname}: {e}")


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------

def normalize(df):
    """Normalize old and new Citibike schemas to unified columns.

    Returns DataFrame with:
        started_at, ended_at             — pandas Timestamp
        start_station_id, end_station_id — str
        is_member, is_casual             — bool
        is_ebike, is_classic             — bool
    """
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

    # Drop rows missing critical fields
    df = df.dropna(subset=['started_at', 'ended_at', 'start_station_id', 'end_station_id'])

    # Drop rows with blank station IDs (dockless trips)
    df['start_station_id'] = df['start_station_id'].astype(str).str.strip()
    df['end_station_id']   = df['end_station_id'].astype(str).str.strip()
    df = df[(df['start_station_id'] != '') & (df['start_station_id'] != 'nan')]
    df = df[(df['end_station_id']   != '') & (df['end_station_id']   != 'nan')]

    # Skip COVID year
    df = df[~df['started_at'].dt.year.isin(SKIP_YEARS)]

    return df


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def aggregate(df):
    """Aggregate a single month's DataFrame into station-hour rows.

    Returns DataFrame with columns:
        station_id, hour, departures, arrivals,
        member_trips, casual_trips, ebike_trips, classic_trips
    """
    if df.empty:
        return pd.DataFrame()

    dep_hour = df['started_at'].dt.floor('h')
    arr_hour = df['ended_at'].dt.floor('h')

    dep = (
        df.assign(hour=dep_hour)
        .groupby(['start_station_id', 'hour'])
        .agg(
            departures=('is_member',   'count'),
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
    """Upsert aggregated station-hour rows into station_hourly_flow."""
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
    print("\nRecomputing station_demand_profile...")
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

def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FOLDER

    if not os.path.isdir(folder):
        print(f"ERROR: folder not found: {folder}")
        sys.exit(1)

    conn      = get_conn()
    completed = load_checkpoint()

    entries = list(scan_zips(folder))
    if not entries:
        print("ERROR: no zip files found in folder.")
        sys.exit(1)

    skipped = sum(1 for _, csv_name in entries
                  if os.path.basename(csv_name) in completed)
    if skipped:
        print(f"Resuming — skipping {skipped} already-completed file(s).")
    print(f"Found {len(entries)} CSV(s) across all zips, "
          f"{len(entries) - skipped} to process.\n")

    for zip_path, csv_name in entries:
        key = os.path.basename(csv_name)

        if key in completed:
            print(f"Skipping {key} (already done)")
            continue

        print(f"Processing {key}  [{os.path.basename(zip_path)}]...")

        try:
            with zipfile.ZipFile(zip_path) as zf:
                with zf.open(csv_name) as f:
                    df = pd.read_csv(f, low_memory=False)
        except Exception as e:
            print(f"  Error reading {key}: {e} — skipping.")
            save_checkpoint(key)
            continue

        df  = normalize(df)
        agg = aggregate(df)

        if agg.empty:
            print(f"  No valid rows (all 2020 or missing station IDs?) — skipping.")
            save_checkpoint(key)
            continue

        inserted = upsert_hourly_flow(conn, agg)
        print(f"  Upserted {inserted:,} station-hour rows.")
        save_checkpoint(key)

    recompute_demand_profile(conn)
    recompute_trip_features(conn)

    conn.close()
    print("\nAll done.")
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("Checkpoint file removed.")


if __name__ == '__main__':
    main()

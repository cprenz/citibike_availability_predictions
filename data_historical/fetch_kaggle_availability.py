import io
import os
import sys
import time
import zipfile
import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'data_ingestion', '.env'))

# One-time backfill: loads the Kaggle "Citi Bike Stations" dataset
# (rosenthal/citi-bike-stations) into station_status_pre2021.
#
# Usage:
#   python fetch_kaggle_availability.py <path-to-zip-or-csv>
#
#   Download the dataset manually from:
#   https://www.kaggle.com/datasets/rosenthal/citi-bike-stations
#   then pass the downloaded zip (or extracted CSV) as the argument.
#
# Uses PostgreSQL COPY for fast bulk loading.
# Checkpoints completed files — safe to resume if interrupted (re-run same command).
# Skips 2020 rows (COVID anomaly).
# 2016-2017 rows will have num_ebikes_available = NULL (e-bikes didn't exist yet).
#
# ---------------------------------------------------------------------------
# Source-data quirks this loader had to handle (kept here so the script's shape
# makes sense and the same gotchas are easy to spot if the loader is re-run or
# adapted to a new dataset):
#
# 1. Timestamp column name — the Kaggle CSVs call it `station_status_last_reported`,
#    not `last_reported`/`fetched_at`. Mapped via COLUMN_MAP.
# 2. `\N` nulls — CSVs use PostgreSQL-dump `\N` for nulls; pandas reads them as the
#    literal string "\N". Handled with na_values=[r'\N'] in pd.read_csv().
# 3. Unix-seconds timestamps — `station_status_last_reported` is Unix seconds
#    (e.g. 1547045888). pd.to_datetime defaults to nanoseconds → 1970 dates.
#    Parse with unit='s', utc=True when the column is numeric.
# 4. Boolean columns as floats — a bool column with NaN becomes float64
#    (1.0/0.0/NaN); execute_values can't cast float→bool. Map to
#    None if pd.isna(x) else ('t' if x else 'f') before COPY.
# 5. Integer columns as floats — int column with NaN becomes float64; COPY rejects
#    "0.0" for an INTEGER column. Map to '' if pd.isna(x) else str(int(x)).
# 6. Duplicate (fetched_at, station_id) rows exist in the source CSVs, so a direct
#    COPY into station_status_pre2021 hits UniqueViolation. Fixed with a staging
#    table: COPY into UNLOGGED _tmp_kaggle_load, then
#    INSERT ... SELECT DISTINCT ON (fetched_at, station_id) ... ON CONFLICT DO NOTHING.
# 7. Entry point — needs an `if __name__ == '__main__': main()` guard, or the script
#    runs silently (exit 0) and does nothing.
# 8. Stale .pyc — a cached __pycache__ .pyc once masked an edited version of this
#    script; delete the .pyc if edits appear to have no effect.
# 9. Duplicate background processes — verify/kill old runs by PID (e.g.
#    Get-WmiObject Win32_Process) before starting a new one; an un-killed earlier
#    run was inserting bad data alongside the fixed run.
# ---------------------------------------------------------------------------

CHUNK_SIZE = 100_000
SKIP_YEARS = {2020}
CHECKPOINT_FILE = os.path.join(os.path.dirname(__file__), 'kaggle_load_checkpoint.txt')

COLUMN_MAP = {
    'station_id':                       'station_id',
    'last_reported':                    'fetched_at',
    'station_status_last_reported':     'fetched_at',
    'fetched_at':                       'fetched_at',
    'time':                             'fetched_at',
    'timestamp':                        'fetched_at',
    'num_bikes_available':              'num_bikes_available',
    'bikes_available':                  'num_bikes_available',
    'num_ebikes_available':             'num_ebikes_available',
    'ebikes_available':                 'num_ebikes_available',
    'num_docks_available':              'num_docks_available',
    'docks_available':                  'num_docks_available',
    'num_bikes_disabled':               'num_bikes_disabled',
    'bikes_disabled':                   'num_bikes_disabled',
    'num_docks_disabled':               'num_docks_disabled',
    'docks_disabled':                   'num_docks_disabled',
    'is_installed':                     'is_installed',
    'is_renting':                       'is_renting',
    'is_returning':                     'is_returning',
}

REQUIRED = {'fetched_at', 'station_id', 'num_bikes_available', 'num_docks_available'}

TARGET_COLS = [
    'fetched_at', 'station_id',
    'num_bikes_available', 'num_ebikes_available',
    'num_docks_available', 'num_bikes_disabled',
    'num_docks_disabled', 'is_installed',
    'is_renting', 'is_returning',
]


def get_conn():
    return psycopg2.connect(
        host=os.getenv('PGHOST'),
        port=int(os.getenv('PGPORT')),
        dbname=os.getenv('PGDATABASE'),
        user=os.getenv('PGUSER'),
        password=os.getenv('PGPASSWORD'),
    )


def create_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS station_status_pre2021 (
                fetched_at              TIMESTAMPTZ  NOT NULL,
                station_id              VARCHAR(50)  NOT NULL,
                num_bikes_available     INTEGER,
                num_ebikes_available    INTEGER,
                num_docks_available     INTEGER,
                num_bikes_disabled      INTEGER,
                num_docks_disabled      INTEGER,
                is_installed            BOOLEAN,
                is_renting              BOOLEAN,
                is_returning            BOOLEAN
            )
        """)
        try:
            cur.execute("SELECT create_hypertable('station_status_pre2021', 'fetched_at', if_not_exists => TRUE)")
        except Exception:
            pass
    conn.commit()
    print("Table station_status_pre2021 ready.")


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return set(f.read().splitlines())
    return set()


def save_checkpoint(filename):
    with open(CHECKPOINT_FILE, 'a') as f:
        f.write(filename + '\n')


def find_csvs(directory):
    csvs = []
    for root, _, files in os.walk(directory):
        for f in sorted(files):
            if f.endswith('.csv'):
                csvs.append(os.path.join(root, f))
    return csvs


def map_columns(df):
    rename = {c: COLUMN_MAP[c] for c in df.columns if c in COLUMN_MAP}
    df = df.rename(columns=rename)

    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"Required columns missing after mapping: {missing}\n"
                         f"Raw columns were: {list(df.columns)}")

    for col in TARGET_COLS:
        if col not in df.columns:
            df[col] = None

    return df[TARGET_COLS]


def prepare_chunk(df):
    df = df.copy()

    # Parse Unix timestamps (seconds) or ISO strings
    if pd.api.types.is_numeric_dtype(df['fetched_at']):
        df['fetched_at'] = pd.to_datetime(df['fetched_at'], unit='s', utc=True, errors='coerce')
    else:
        df['fetched_at'] = pd.to_datetime(df['fetched_at'], utc=True, errors='coerce')

    df = df.dropna(subset=['fetched_at', 'station_id'])
    df = df[~df['fetched_at'].dt.year.isin(SKIP_YEARS)]
    if df.empty:
        return df

    # Format timestamp as ISO string for COPY
    df['fetched_at'] = df['fetched_at'].dt.strftime('%Y-%m-%d %H:%M:%S+00')

    # Integer columns: floats like 0.0 -> '0', NaN -> '' (COPY NULL placeholder)
    for col in ('num_bikes_available', 'num_ebikes_available', 'num_docks_available',
                'num_bikes_disabled', 'num_docks_disabled'):
        if col in df.columns:
            df[col] = df[col].apply(lambda x: '' if pd.isna(x) else str(int(x)))

    # Boolean columns: 1.0/0.0/NaN -> t/f/empty for COPY
    for col in ('is_installed', 'is_renting', 'is_returning'):
        if col in df.columns:
            df[col] = df[col].map(lambda x: '' if pd.isna(x) else ('t' if x else 'f'))

    return df


def copy_chunk(conn, df):
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep='')
    buf.seek(0)
    with conn.cursor() as cur:
        cur.copy_expert(
            "COPY station_status_pre2021 ("
            "fetched_at, station_id, num_bikes_available, num_ebikes_available, "
            "num_docks_available, num_bikes_disabled, num_docks_disabled, "
            "is_installed, is_renting, is_returning"
            ") FROM STDIN WITH (FORMAT CSV, NULL '')",
            buf
        )
    conn.commit()
    return len(df)


def reconnect(retries=10, delay=30):
    """Wait for the DB to come back up and return a fresh connection."""
    for attempt in range(1, retries + 1):
        print(f"  DB connection lost — reconnect attempt {attempt}/{retries} in {delay}s...")
        time.sleep(delay)
        try:
            conn = get_conn()
            print("  Reconnected.")
            return conn
        except Exception as e:
            print(f"  Failed: {e}")
    print("ERROR: could not reconnect after {retries} attempts. Exiting.")
    sys.exit(1)


def load_csv(conn, path, display_name=None):
    name = display_name or os.path.basename(path)
    print(f"\nLoading {name}...")
    total = 0
    first_chunk = True

    for chunk in pd.read_csv(path, chunksize=CHUNK_SIZE, low_memory=False, na_values=[r'\N']):
        if first_chunk:
            print(f"  Columns: {list(chunk.columns)}")
            first_chunk = False

        chunk = map_columns(chunk)
        chunk = prepare_chunk(chunk)
        if chunk.empty:
            continue

        while True:
            try:
                inserted = copy_chunk(conn, chunk)
                break
            except psycopg2.OperationalError:
                conn = reconnect()

        total += inserted
        print(f"  {total:,} rows inserted...")

    print(f"  Done: {total:,} rows")
    return conn, total


def main():
    if len(sys.argv) < 2:
        print("Usage: python fetch_kaggle_availability.py <path-to-zip-or-csv>")
        sys.exit(1)

    input_path = sys.argv[1]
    if not os.path.exists(input_path):
        print(f"ERROR: path not found: {input_path}")
        sys.exit(1)

    conn = get_conn()
    create_table(conn)

    completed = load_checkpoint()
    grand_total = 0

    if input_path.endswith('.zip'):
        with zipfile.ZipFile(input_path, 'r') as zf:
            entries = sorted(e for e in zf.namelist() if e.endswith('.csv'))
            if not entries:
                print("ERROR: No CSV files found in zip.")
                sys.exit(1)
            skipped = sum(1 for e in entries if os.path.basename(e) in completed)
            if skipped:
                print(f"Resuming — skipping {skipped} already-completed file(s).")
            print(f"Found {len(entries)} CSV file(s) in zip, {len(entries) - skipped} to process.\n")
            for entry in entries:
                name = os.path.basename(entry)
                if name in completed:
                    print(f"Skipping {name} (already done)")
                    continue
                with zf.open(entry) as f:
                    conn, rows = load_csv(conn, f, display_name=name)
                grand_total += rows
                save_checkpoint(name)
    elif os.path.isdir(input_path):
        csvs = find_csvs(input_path)
        if not csvs:
            print("ERROR: No CSV files found.")
            sys.exit(1)
        skipped = sum(1 for c in csvs if os.path.basename(c) in completed)
        if skipped:
            print(f"Resuming — skipping {skipped} already-completed file(s).")
        print(f"Found {len(csvs)} CSV file(s), {len(csvs) - skipped} to process.\n")
        for csv_path in csvs:
            name = os.path.basename(csv_path)
            if name in completed:
                print(f"Skipping {name} (already done)")
                continue
            conn, rows = load_csv(conn, csv_path)
            grand_total += rows
            save_checkpoint(name)
    elif input_path.endswith('.csv'):
        name = os.path.basename(input_path)
        if name not in completed:
            conn, rows = load_csv(conn, input_path)
            grand_total += rows
            save_checkpoint(name)
    else:
        print(f"ERROR: unrecognised input type: {input_path}")
        sys.exit(1)

    conn.close()
    print(f"\nDone. {grand_total:,} total rows inserted into station_status_pre2021.")
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("Checkpoint file removed.")


if __name__ == '__main__':
    main()

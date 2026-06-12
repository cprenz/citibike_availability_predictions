import os
import requests
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'data_ingestion', '.env'))

API_URL = "https://data.ny.gov/resource/i9wp-a4ja.json"
PAGE_SIZE = 1000


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def fetch_page(offset):
    params = {
        "$limit": PAGE_SIZE,
        "$offset": offset,
        "$where": "entry_allowed='YES' OR exit_allowed='YES'",
    }
    r = requests.get(API_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def is_emergency_only(row):
    entry = str(row.get("entry_allowed", "")).upper() == "YES"
    exit_ = str(row.get("exit_allowed", "")).upper() == "YES"
    etype = str(row.get("entrance_type", "")).lower()
    return not entry and exit_ and "emergency" in etype


def insert_rows(conn, rows):
    records = []
    for row in rows:
        if is_emergency_only(row):
            continue
        try:
            lat = float(row["entrance_latitude"])
            lon = float(row["entrance_longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        records.append((
            int(row["station_id"]) if row.get("station_id") else None,
            int(row["complex_id"]) if row.get("complex_id") else None,
            row.get("gtfs_stop_id"),
            row.get("constituent_station_name") or row.get("stop_name"),
            row.get("daytime_routes"),
            row.get("line"),
            row.get("division"),
            row.get("borough"),
            row.get("entrance_type"),
            str(row.get("entry_allowed", "")).upper() == "YES",
            str(row.get("exit_allowed", "")).upper() == "YES",
            lat,
            lon,
        ))
    if records:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO mta_subway_entrances (
                    station_id, complex_id, gtfs_stop_id, constituent_station_name,
                    daytime_routes, line, division, borough, entrance_type,
                    entry_allowed, exit_allowed, entrance_latitude, entrance_longitude
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, records)
        conn.commit()
    return len(records)


def main():
    conn = get_conn()
    offset = 0
    total = 0
    print("Fetching MTA subway entrances from NYC Open Data...")
    while True:
        rows = fetch_page(offset)
        if not rows:
            break
        inserted = insert_rows(conn, rows)
        total += inserted
        print(f"  offset={offset}  inserted={inserted}  running total={total}")
        offset += PAGE_SIZE
    conn.close()
    print(f"Done. {total} rows inserted into mta_subway_entrances.")


if __name__ == "__main__":
    main()

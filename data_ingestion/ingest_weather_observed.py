import os
import time
import requests
import psycopg2
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# Runs daily via Windows Task Scheduler.
# Fetches the last 10 days of observed weather from Open-Meteo and appends
# to weather_post2021_openmeteo_observed.
# Retries up to 3 times on failure with exponential backoff.
# Logs every run (success or failure) to weather_ingestion_log.

SCRIPT_NAME = "ingest_weather_observed"
NYC_LAT = 40.7128
NYC_LON = -74.0060
BASE_URL = "https://archive-api.open-meteo.com/v1/archive"
LOOKBACK_DAYS = 10
MAX_RETRIES = 3
VARIABLES = ",".join([
    "temperature_2m", "apparent_temperature", "precipitation", "rain",
    "snowfall", "wind_speed_10m", "wind_direction_10m", "cloud_cover",
    "relative_humidity_2m", "dewpoint_2m", "surface_pressure",
])


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def log(status, rows_inserted=None, error_message=None):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO weather_ingestion_log (script, status, rows_inserted, error_message)
                VALUES (%s, %s, %s, %s)
            """, (SCRIPT_NAME, status, rows_inserted, error_message))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: could not write to weather_ingestion_log: {e}")


def fetch(start, end):
    params = {
        "latitude": NYC_LAT,
        "longitude": NYC_LON,
        "start_date": start,
        "end_date": end,
        "hourly": VARIABLES,
        "timezone": "UTC",
    }
    r = requests.get(BASE_URL, params=params, timeout=60)
    r.raise_for_status()
    return r.json()["hourly"]


def insert(conn, hourly):
    rows = list(zip(
        hourly["time"],
        hourly["temperature_2m"],
        hourly["apparent_temperature"],
        hourly["precipitation"],
        hourly["rain"],
        hourly["snowfall"],
        hourly["wind_speed_10m"],
        hourly["wind_direction_10m"],
        hourly["cloud_cover"],
        hourly["relative_humidity_2m"],
        hourly["dewpoint_2m"],
        hourly["surface_pressure"],
    ))
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO weather_post2021_openmeteo_observed (
                timestamp, temperature_2m, apparent_temperature, precipitation, rain,
                snowfall, wind_speed_10m, wind_direction_10m, cloud_cover,
                relative_humidity_2m, dewpoint_2m, surface_pressure
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (timestamp) DO NOTHING
        """, rows)
    conn.commit()
    return len(rows)


def main():
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=LOOKBACK_DAYS)
    print(f"Fetching observed weather {start} → {end}...")

    conn = get_conn()
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            hourly = fetch(start.isoformat(), end.isoformat())
            inserted = insert(conn, hourly)
            log(status="success", rows_inserted=inserted)
            print(f"Done. {inserted} rows inserted.")
            conn.close()
            return
        except Exception as e:
            last_error = str(e)
            wait = 60 * (2 ** (attempt - 1))  # 60s, 120s, 240s
            print(f"Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                print(f"Retrying in {wait}s...")
                time.sleep(wait)

    log(status="error", error_message=last_error)
    conn.close()
    print(f"All {MAX_RETRIES} attempts failed. Logged to weather_ingestion_log.")


if __name__ == "__main__":
    main()

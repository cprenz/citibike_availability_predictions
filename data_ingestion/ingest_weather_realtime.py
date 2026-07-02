import os
import time
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# Runs hourly via Windows Task Scheduler (CitibikeWeatherRealtime).
# Fetches the last 6 hours of analyzed weather from Open-Meteo's forecast API
# and upserts to weather_realtime. Separate from weather_post2021_openmeteo_observed
# (ERA5, ~5 day lag) so the two sources never collide. score_stations.py queries
# this table first for current conditions, falls back to ERA5 if empty.
# ON CONFLICT DO UPDATE so refined model values overwrite earlier estimates.

SCRIPT_NAME = "ingest_weather_realtime"
NYC_LAT = 40.7128
NYC_LON = -74.0060
REALTIME_URL = "https://api.open-meteo.com/v1/forecast"
PAST_HOURS = 6
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


def log(conn, status, rows_inserted=None, error_message=None):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO weather_ingestion_log (script, status, rows_inserted, error_message)
                VALUES (%s, %s, %s, %s)
            """, (SCRIPT_NAME, status, rows_inserted, error_message))
        conn.commit()
    except Exception as e:
        print(f"Warning: could not write to weather_ingestion_log: {e}")


def fetch():
    # forecast_days=0 is not honored by this endpoint — it still returns its full
    # default forecast window (~16 days) alongside past_hours. Request forecast_days=1
    # (the API minimum that behaves predictably) and trim to the current hour in
    # insert() instead of trusting the API to restrict the window.
    params = {
        "latitude": NYC_LAT,
        "longitude": NYC_LON,
        "hourly": VARIABLES,
        "past_hours": PAST_HOURS,
        "forecast_days": 1,
        "timezone": "UTC",
    }
    r = requests.get(REALTIME_URL, params=params, timeout=60)
    r.raise_for_status()
    return r.json()["hourly"]


def insert(conn, hourly):
    # Trim client-side to the intended past_hours + current-hour window — the API's
    # forecast_days param doesn't reliably restrict how far forward it returns data,
    # so don't trust the response window as-is.
    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    cutoff = now_hour + timedelta(hours=1)  # allow the current hour itself through

    rows = []
    for row in zip(
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
    ):
        row_time = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
        if row_time < cutoff:
            rows.append(row)

    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO weather_realtime (
                timestamp, temperature_2m, apparent_temperature, precipitation, rain,
                snowfall, wind_speed_10m, wind_direction_10m, cloud_cover,
                relative_humidity_2m, dewpoint_2m, surface_pressure
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (timestamp) DO UPDATE SET
                temperature_2m       = EXCLUDED.temperature_2m,
                apparent_temperature = EXCLUDED.apparent_temperature,
                precipitation        = EXCLUDED.precipitation,
                rain                 = EXCLUDED.rain,
                snowfall             = EXCLUDED.snowfall,
                wind_speed_10m       = EXCLUDED.wind_speed_10m,
                wind_direction_10m   = EXCLUDED.wind_direction_10m,
                cloud_cover          = EXCLUDED.cloud_cover,
                relative_humidity_2m = EXCLUDED.relative_humidity_2m,
                dewpoint_2m          = EXCLUDED.dewpoint_2m,
                surface_pressure     = EXCLUDED.surface_pressure;
        """, rows)
    conn.commit()
    return len(rows)


def main():
    print(f"Fetching near-realtime weather (past {PAST_HOURS}h)...")
    conn = get_conn()
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            hourly = fetch()
            inserted = insert(conn, hourly)
            log(conn, status="success", rows_inserted=inserted)
            print(f"Done. {inserted} rows upserted to weather_realtime.")
            conn.close()
            return
        except Exception as e:
            last_error = str(e)
            wait = 60 * (2 ** (attempt - 1))
            print(f"Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                print(f"Retrying in {wait}s...")
                time.sleep(wait)

    log(conn, status="error", error_message=last_error)
    conn.close()
    print(f"All {MAX_RETRIES} attempts failed. Logged to weather_ingestion_log.")


if __name__ == "__main__":
    main()

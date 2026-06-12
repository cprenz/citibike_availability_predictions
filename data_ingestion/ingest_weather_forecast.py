import os
import time
import requests
import psycopg2
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# Runs daily via Windows Task Scheduler.
# Fetches the last 2 weeks of forecast runs from Open-Meteo's Historical
# Forecast API and appends to weather_post2021_openmeteo_forecast.
# Retries up to 3 times per run on failure with exponential backoff.
# Logs every run (success or failure) to weather_ingestion_log.

SCRIPT_NAME = "ingest_weather_forecast"
NYC_LAT = 40.7128
NYC_LON = -74.0060
BASE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 7
LOOKBACK_WEEKS = 2
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


def last_n_mondays(n):
    today = date.today()
    most_recent = today - timedelta(days=today.weekday())
    return [most_recent - timedelta(weeks=i) for i in range(n - 1, -1, -1)]


def fetch_run(run_date):
    end_date = min(run_date + timedelta(days=FORECAST_DAYS - 1), date.today() - timedelta(days=1))
    params = {
        "latitude": NYC_LAT,
        "longitude": NYC_LON,
        "start_date": run_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": VARIABLES,
        "timezone": "UTC",
    }
    r = requests.get(BASE_URL, params=params, timeout=60)
    r.raise_for_status()
    return r.json()["hourly"]


def insert(conn, run_date, hourly):
    run_ts = f"{run_date.isoformat()}T00:00"
    rows = []
    for i, valid_ts in enumerate(hourly["time"]):
        valid_dt = date.fromisoformat(valid_ts[:10])
        lead_hours = (valid_dt - run_date).days * 24 + int(valid_ts[11:13])
        rows.append((
            run_ts, valid_ts, lead_hours,
            hourly["temperature_2m"][i],
            hourly["apparent_temperature"][i],
            hourly["precipitation"][i],
            hourly["rain"][i],
            hourly["snowfall"][i],
            hourly["wind_speed_10m"][i],
            hourly["wind_direction_10m"][i],
            hourly["cloud_cover"][i],
            hourly["relative_humidity_2m"][i],
            hourly["dewpoint_2m"][i],
            hourly["surface_pressure"][i],
        ))
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO weather_post2021_openmeteo_forecast (
                run_time, valid_time, lead_time_hours,
                temperature_2m, apparent_temperature, precipitation, rain,
                snowfall, wind_speed_10m, wind_direction_10m, cloud_cover,
                relative_humidity_2m, dewpoint_2m, surface_pressure
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_time, valid_time) DO NOTHING
        """, rows)
    conn.commit()
    return len(rows)


def fetch_with_retry(run_date):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fetch_run(run_date)
        except Exception as e:
            last_error = str(e)
            wait = 60 * (2 ** (attempt - 1))
            print(f"  Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                print(f"  Retrying in {wait}s...")
                time.sleep(wait)
    raise Exception(last_error)


def main():
    conn = get_conn()
    total_inserted = 0
    errors = []

    for run_date in last_n_mondays(LOOKBACK_WEEKS):
        print(f"Fetching forecast run {run_date}...")
        try:
            hourly = fetch_with_retry(run_date)
            inserted = insert(conn, run_date, hourly)
            total_inserted += inserted
            print(f"  {inserted} rows inserted")
        except Exception as e:
            errors.append(f"{run_date}: {e}")
            print(f"  Failed after {MAX_RETRIES} attempts: {e}")
        time.sleep(1)

    if errors:
        error_msg = " | ".join(errors)
        status = "partial" if total_inserted > 0 else "error"
        log(status=status, rows_inserted=total_inserted, error_message=error_msg)
        print(f"Completed with errors. {total_inserted} rows inserted.")
    else:
        log(status="success", rows_inserted=total_inserted)
        print(f"Done. {total_inserted} rows inserted.")

    conn.close()


if __name__ == "__main__":
    main()

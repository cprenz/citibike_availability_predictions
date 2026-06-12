import os
import time
import requests
import psycopg2
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'data_ingestion', '.env'))

# Open-Meteo Historical Forecast API — covers 2015 through today.
# Originally scoped to post-2021 only, but the API confirmed it supports
# pre-2021 dates, so NCAR GFS is not needed.
#
# Iterates weekly from 2015-01-05 to today. Each week's Monday is treated
# as the model run time (run_time). Hourly forecast steps for that week are
# stored as (run_time, valid_time, lead_time_hours).
#
# Writes to:
#   weather_pre2021_gfs_forecast         for run dates before 2021-01-01
#   weather_post2021_openmeteo_forecast  for run dates 2021-01-01 and later
#
# No account needed. Safe to re-run — uses ON CONFLICT DO NOTHING.
# ~570 weekly API calls for 2015–present. Runs in ~10 minutes at 1 call/sec.

NYC_LAT = 40.7128
NYC_LON = -74.0060
BASE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 7
CUTOFF = date(2021, 1, 1)
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


def target_table(run_date):
    return "weather_pre2021_gfs_forecast" if run_date < CUTOFF else "weather_post2021_openmeteo_forecast"


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


def has_data(hourly):
    return any(v is not None for v in hourly.get("temperature_2m", []))


def insert(conn, run_date, hourly):
    table = target_table(run_date)
    run_ts = f"{run_date.isoformat()}T00:00"
    rows = []
    for i, valid_ts in enumerate(hourly["time"]):
        valid_dt = date.fromisoformat(valid_ts[:10])
        lead_hours = (valid_dt - run_date).days * 24 + int(valid_ts[11:13])
        rows.append((
            run_ts,
            valid_ts,
            lead_hours,
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
        cur.executemany(f"""
            INSERT INTO {table} (
                run_time, valid_time, lead_time_hours,
                temperature_2m, apparent_temperature, precipitation, rain,
                snowfall, wind_speed_10m, wind_direction_10m, cloud_cover,
                relative_humidity_2m, dewpoint_2m, surface_pressure
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_time, valid_time) DO NOTHING
        """, rows)
    conn.commit()
    return len(rows), table


def weekly_mondays(start=date(2018, 1, 1)):
    # Open-Meteo historical forecast coverage starts in 2018.
    # 2015-2017 rows in weather_pre2021_gfs_forecast will have no forecast
    # features — those training rows will use observed weather as a proxy.
    d = start
    today = date.today()
    while d < today:
        yield d
        d += timedelta(weeks=1)


def main():
    conn = get_conn()
    for run_date in weekly_mondays():
        try:
            hourly = fetch_run(run_date)
            if not has_data(hourly):
                print(f"{run_date}  skipped (no data)")
                continue
            inserted, table = insert(conn, run_date, hourly)
            print(f"{run_date}  →  {table}  ({inserted} rows)")
        except Exception as e:
            print(f"{run_date}  ERROR: {e}")
        time.sleep(1)
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()

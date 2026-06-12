import os
import time
import requests
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'data_ingestion', '.env'))

# ERA5 reanalysis via Open-Meteo archive API — covers 1940 to ~5 days ago.
# No account needed. Pulls 2015-01-01 through 2020-12-31 into
# weather_pre2021_era5_observed. Fetches one year at a time to stay within
# API size limits. Safe to re-run — uses ON CONFLICT DO NOTHING.

NYC_LAT = 40.7128
NYC_LON = -74.0060
BASE_URL = "https://archive-api.open-meteo.com/v1/archive"
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


def fetch_year(year):
    params = {
        "latitude": NYC_LAT,
        "longitude": NYC_LON,
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
        "hourly": VARIABLES,
        "timezone": "UTC",
    }
    r = requests.get(BASE_URL, params=params, timeout=120)
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
            INSERT INTO weather_pre2021_era5_observed (
                timestamp, temperature_2m, apparent_temperature, precipitation, rain,
                snowfall, wind_speed_10m, wind_direction_10m, cloud_cover,
                relative_humidity_2m, dewpoint_2m, surface_pressure
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (timestamp) DO NOTHING
        """, rows)
    conn.commit()
    return len(rows)


def main():
    conn = get_conn()
    for year in range(2015, 2021):
        print(f"Fetching ERA5 observed weather for {year}...")
        hourly = fetch_year(year)
        inserted = insert(conn, hourly)
        print(f"  {inserted} rows inserted")
        time.sleep(1)
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()

import os
import psycopg2
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Runs daily via Task Scheduler after ingest_trip_monthly.py.
# Aggregates station_hourly_flow into station_daily_ridership (one row per
# station per day). Only processes dates not yet in the table — safe to re-run.
#
# Borough is derived from lat/lon bounding boxes. station_hourly_flow.hour is
# stored in local NYC time (as TIMESTAMPTZ), so DATE(hour) gives the correct
# NYC calendar date directly.
#
# The LEFT JOIN to station_information uses short_name, which matches 2026
# station IDs. Pre-2021 integer IDs (e.g. "116") won't match and get NULL
# borough/name — acceptable since Tableau visualizations focus on live data.

UPSERT_SQL = """
INSERT INTO station_daily_ridership (
    station_id, date, station_name, borough, lat, lon, capacity,
    total_departures, total_arrivals, net_flow,
    ebike_departures, classic_departures, ebike_pct, classic_pct,
    member_trips, casual_trips, member_pct, casual_pct,
    avg_hourly_departures
)
SELECT
    f.station_id,
    DATE(f.hour)                                                        AS date,
    si.name                                                             AS station_name,
    sb.borough                                                          AS borough,
    si.lat,
    si.lon,
    si.capacity,
    SUM(f.departures)                                                   AS total_departures,
    SUM(f.arrivals)                                                     AS total_arrivals,
    SUM(f.arrivals) - SUM(f.departures)                                 AS net_flow,
    SUM(f.ebike_trips)                                                  AS ebike_departures,
    SUM(f.classic_trips)                                                AS classic_departures,
    ROUND(100.0 * SUM(f.ebike_trips)   / NULLIF(SUM(f.departures), 0), 1) AS ebike_pct,
    ROUND(100.0 * SUM(f.classic_trips) / NULLIF(SUM(f.departures), 0), 1) AS classic_pct,
    SUM(f.member_trips)                                                 AS member_trips,
    SUM(f.casual_trips)                                                 AS casual_trips,
    ROUND(100.0 * SUM(f.member_trips)  / NULLIF(SUM(f.departures), 0), 1) AS member_pct,
    ROUND(100.0 * SUM(f.casual_trips)  / NULLIF(SUM(f.departures), 0), 1) AS casual_pct,
    ROUND(SUM(f.departures) / 24.0, 2)                                 AS avg_hourly_departures
FROM station_hourly_flow f
LEFT JOIN station_information si ON f.station_id = si.short_name
LEFT JOIN station_borough sb ON si.station_id = sb.station_id
WHERE DATE(f.hour) >= %s AND DATE(f.hour) < %s
GROUP BY
    f.station_id, DATE(f.hour), si.name,
    sb.borough, si.lat, si.lon, si.capacity
ON CONFLICT (station_id, date) DO UPDATE SET
    station_name          = EXCLUDED.station_name,
    borough               = EXCLUDED.borough,
    lat                   = EXCLUDED.lat,
    lon                   = EXCLUDED.lon,
    capacity              = EXCLUDED.capacity,
    total_departures      = EXCLUDED.total_departures,
    total_arrivals        = EXCLUDED.total_arrivals,
    net_flow              = EXCLUDED.net_flow,
    ebike_departures      = EXCLUDED.ebike_departures,
    classic_departures    = EXCLUDED.classic_departures,
    ebike_pct             = EXCLUDED.ebike_pct,
    classic_pct           = EXCLUDED.classic_pct,
    member_trips          = EXCLUDED.member_trips,
    casual_trips          = EXCLUDED.casual_trips,
    member_pct            = EXCLUDED.member_pct,
    casual_pct            = EXCLUDED.casual_pct,
    avg_hourly_departures = EXCLUDED.avg_hourly_departures;
"""


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def get_start_date(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(date) FROM station_daily_ridership;")
        max_date = cur.fetchone()[0]
    if max_date is None:
        # First run — go back to the start of station_hourly_flow
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(DATE(hour)) FROM station_hourly_flow;")
            return cur.fetchone()[0]
    # Re-process the last date (may be incomplete) plus everything after it
    return max_date


def main():
    conn = get_conn()
    start_date = get_start_date(conn)
    end_date = date.today()  # exclude today — hourly data may still be arriving

    if start_date >= end_date:
        print("station_daily_ridership is already up to date.")
        conn.close()
        return

    print(f"Building station_daily_ridership from {start_date} to {end_date} (exclusive)...")
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, (start_date, end_date))
        rows = cur.rowcount
    conn.commit()
    conn.close()
    print(f"Done. {rows} rows upserted.")


if __name__ == "__main__":
    main()

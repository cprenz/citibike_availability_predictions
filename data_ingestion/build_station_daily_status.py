import os
import psycopg2
from datetime import date
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Runs daily via Task Scheduler after build_station_daily_ridership.py.
# Aggregates station_status_hourly_clean into station_daily_status (one row per
# station per day). Only processes dates not yet in the table — safe to re-run.
#
# station_status_hourly_clean.hour is in UTC, so we convert to NYC local time
# before taking the calendar date. This ensures a station active at 11pm NYC
# time is counted in the correct NYC calendar day, not the next UTC day.
#
# Borough derivation uses the same bounding boxes as build_station_daily_ridership.py.
# station_status_hourly_clean stores full UUID station IDs, so we JOIN on
# station_information.station_id (not short_name). Pre-2021 legacy integer IDs
# won't match and get NULL metadata — acceptable since Tableau focuses on live data.

UPSERT_SQL = """
INSERT INTO station_daily_status (
    station_id, date, station_name, borough, lat, lon, capacity,
    avg_bikes_available, min_bikes_available, max_bikes_available,
    avg_ebikes_available, avg_classic_available,
    avg_docks_available, avg_bikes_disabled,
    avg_fill_ratio, min_fill_ratio, max_fill_ratio,
    hours_sampled
)
SELECT
    s.station_id,
    DATE(s.hour AT TIME ZONE 'America/New_York')                              AS date,
    si.name                                                                    AS station_name,
    sb.borough                                                                 AS borough,
    si.lat,
    si.lon,
    s.capacity,
    ROUND(AVG(s.num_bikes_available)::NUMERIC, 2)                             AS avg_bikes_available,
    MIN(s.num_bikes_available)                                                 AS min_bikes_available,
    MAX(s.num_bikes_available)                                                 AS max_bikes_available,
    ROUND(AVG(s.num_ebikes_available)::NUMERIC, 2)                            AS avg_ebikes_available,
    ROUND(AVG(s.num_bikes_available - s.num_ebikes_available)::NUMERIC, 2)    AS avg_classic_available,
    ROUND(AVG(s.num_docks_available)::NUMERIC, 2)                             AS avg_docks_available,
    ROUND(AVG(s.num_bikes_disabled)::NUMERIC, 2)                              AS avg_bikes_disabled,
    ROUND(AVG(s.num_bikes_available::FLOAT / NULLIF(s.capacity, 0))::NUMERIC, 4)   AS avg_fill_ratio,
    ROUND(MIN(s.num_bikes_available::FLOAT / NULLIF(s.capacity, 0))::NUMERIC, 4)   AS min_fill_ratio,
    ROUND(MAX(s.num_bikes_available::FLOAT / NULLIF(s.capacity, 0))::NUMERIC, 4)   AS max_fill_ratio,
    COUNT(*)                                                                   AS hours_sampled
FROM station_status_hourly_clean s
LEFT JOIN station_information si ON s.station_id = si.station_id
LEFT JOIN station_borough sb ON si.station_id = sb.station_id
WHERE DATE(s.hour AT TIME ZONE 'America/New_York') >= %s
  AND DATE(s.hour AT TIME ZONE 'America/New_York') < %s
GROUP BY
    s.station_id,
    DATE(s.hour AT TIME ZONE 'America/New_York'),
    si.name,
    sb.borough, si.lat, si.lon,
    s.capacity
ON CONFLICT (station_id, date) DO UPDATE SET
    station_name          = EXCLUDED.station_name,
    borough               = EXCLUDED.borough,
    lat                   = EXCLUDED.lat,
    lon                   = EXCLUDED.lon,
    capacity              = EXCLUDED.capacity,
    avg_bikes_available   = EXCLUDED.avg_bikes_available,
    min_bikes_available   = EXCLUDED.min_bikes_available,
    max_bikes_available   = EXCLUDED.max_bikes_available,
    avg_ebikes_available  = EXCLUDED.avg_ebikes_available,
    avg_classic_available = EXCLUDED.avg_classic_available,
    avg_docks_available   = EXCLUDED.avg_docks_available,
    avg_bikes_disabled    = EXCLUDED.avg_bikes_disabled,
    avg_fill_ratio        = EXCLUDED.avg_fill_ratio,
    min_fill_ratio        = EXCLUDED.min_fill_ratio,
    max_fill_ratio        = EXCLUDED.max_fill_ratio,
    hours_sampled         = EXCLUDED.hours_sampled;
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
        cur.execute("SELECT MAX(date) FROM station_daily_status;")
        max_date = cur.fetchone()[0]
    if max_date is None:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MIN(DATE(hour AT TIME ZONE 'America/New_York'))
                FROM station_status_hourly_clean;
            """)
            return cur.fetchone()[0]
    return max_date


def main():
    conn = get_conn()
    start_date = get_start_date(conn)
    end_date = date.today()

    if start_date >= end_date:
        print("station_daily_status is already up to date.")
        conn.close()
        return

    print(f"Building station_daily_status from {start_date} to {end_date} (exclusive)...")
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, (start_date, end_date))
        rows = cur.rowcount
    conn.commit()
    conn.close()
    print(f"Done. {rows} rows upserted.")


if __name__ == "__main__":
    main()

import os
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Builds station_hourly_profile: avg rides and avg availability by hour of day
# per station. Static aggregation, rebuilt from scratch each run (TRUNCATE +
# INSERT). Run monthly after new trip CSVs land via ingest_trip_monthly.py.
#
# FUTURE: add a year dimension (station_id, hour_of_day, year) so Tableau can
# let users filter/compare 2019 vs 2021 vs 2026 averages and see the COVID gap
# and ebike rollout side by side. Schema change: add year column to PRIMARY KEY,
# group by year in both CTEs, update snowflake_ddl.sql to match.
#
# Sources:
#   station_hourly_flow  -- trip counts (local NYC time in .hour column)
#   station_status_hourly_clean -- availability snapshots (UTC in .hour column)
#
# Both are averaged over all available data (2019+2021+2026 training years).
# station_status_hourly_clean.hour is converted to NYC local time before
# extracting hour_of_day so the availability profile aligns with trip data.

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS station_hourly_profile (
    station_id              VARCHAR(50)     NOT NULL,
    station_name            VARCHAR(200),
    borough                 VARCHAR(50),
    lat                     FLOAT,
    lon                     FLOAT,
    capacity                INTEGER,
    hour_of_day             SMALLINT        NOT NULL,
    avg_departures          FLOAT,
    avg_arrivals            FLOAT,
    avg_net_flow            FLOAT,
    avg_ebike_departures    FLOAT,
    avg_classic_departures  FLOAT,
    avg_bikes_available     FLOAT,
    avg_ebikes_available    FLOAT,
    avg_fill_ratio          FLOAT,
    PRIMARY KEY (station_id, hour_of_day)
);
"""

TRUNCATE_SQL = "TRUNCATE TABLE station_hourly_profile;"

INSERT_SQL = """
INSERT INTO station_hourly_profile (
    station_id, station_name, borough, lat, lon, capacity,
    hour_of_day,
    avg_departures, avg_arrivals, avg_net_flow,
    avg_ebike_departures, avg_classic_departures,
    avg_bikes_available, avg_ebikes_available, avg_fill_ratio
)
WITH flow AS (
    SELECT
        station_id,
        EXTRACT(HOUR FROM hour)::SMALLINT   AS hour_of_day,
        AVG(departures)                      AS avg_departures,
        AVG(arrivals)                        AS avg_arrivals,
        AVG(departures - arrivals)           AS avg_net_flow,
        AVG(ebike_trips)                     AS avg_ebike_departures,
        AVG(classic_trips)                   AS avg_classic_departures
    FROM station_hourly_flow
    WHERE EXTRACT(YEAR FROM hour) IN (2019, 2021, 2026)
    GROUP BY station_id, EXTRACT(HOUR FROM hour)::SMALLINT
),
status AS (
    SELECT
        s.station_id,
        EXTRACT(HOUR FROM s.hour AT TIME ZONE 'America/New_York')::SMALLINT AS hour_of_day,
        AVG(s.num_bikes_available)                                            AS avg_bikes_available,
        AVG(s.num_ebikes_available)                                           AS avg_ebikes_available,
        AVG(s.num_bikes_available::FLOAT / NULLIF(s.capacity, 0))            AS avg_fill_ratio
    FROM station_status_hourly_clean s
    WHERE EXTRACT(YEAR FROM s.hour AT TIME ZONE 'America/New_York') IN (2019, 2021, 2026)
    GROUP BY s.station_id, EXTRACT(HOUR FROM s.hour AT TIME ZONE 'America/New_York')::SMALLINT
)
SELECT
    COALESCE(f.station_id, st.station_id)   AS station_id,
    si.name                                  AS station_name,
    sb.borough,
    si.lat,
    si.lon,
    si.capacity,
    COALESCE(f.hour_of_day, st.hour_of_day) AS hour_of_day,
    ROUND(f.avg_departures::NUMERIC, 3)      AS avg_departures,
    ROUND(f.avg_arrivals::NUMERIC, 3)        AS avg_arrivals,
    ROUND(f.avg_net_flow::NUMERIC, 3)        AS avg_net_flow,
    ROUND(f.avg_ebike_departures::NUMERIC, 3)   AS avg_ebike_departures,
    ROUND(f.avg_classic_departures::NUMERIC, 3) AS avg_classic_departures,
    ROUND(st.avg_bikes_available::NUMERIC, 3)   AS avg_bikes_available,
    ROUND(st.avg_ebikes_available::NUMERIC, 3)  AS avg_ebikes_available,
    ROUND(st.avg_fill_ratio::NUMERIC, 4)        AS avg_fill_ratio
FROM flow f
FULL OUTER JOIN status st
    ON f.station_id = st.station_id AND f.hour_of_day = st.hour_of_day
LEFT JOIN station_information si
    ON COALESCE(f.station_id, st.station_id) = si.station_id
LEFT JOIN station_borough sb
    ON si.station_id = sb.station_id
ON CONFLICT (station_id, hour_of_day) DO NOTHING;
"""


def get_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST"),
        port=int(os.getenv("PGPORT")),
        dbname=os.getenv("PGDATABASE"),
        user=os.getenv("PGUSER"),
        password=os.getenv("PGPASSWORD"),
    )


def main():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        cur.execute(TRUNCATE_SQL)
        cur.execute(INSERT_SQL)
        rows = cur.rowcount
    conn.commit()
    conn.close()
    print(f"Done. {rows} rows written to station_hourly_profile.")


if __name__ == "__main__":
    main()

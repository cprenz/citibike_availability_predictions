import os
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Builds ride_explorer_profile: one row per (station_id, year, month, day_of_week,
# hour_et), summing trip counts from station_hourly_flow. This is the data source
# for the Ride Explorer 3D map on the web app (web_app/src/components/ThreeDMap.tsx).
#
# WHY THIS EXISTS: the Ride Explorer was originally querying station_hourly_flow
# directly from BigQuery (33M raw rows), which hit BigQuery's no-billing sandbox
# storage quota mid-backfill. Pre-aggregating here to this grain shrinks what
# actually needs to be stored/served in the cloud, and lets us serve it from
# Snowflake instead (already funded via the existing trial, so this adds no new
# billing risk). See sync_ride_explorer_to_snowflake.py for the push step.
#
# TODO — AUTOMATE THIS. Right now this script must be re-run BY HAND after new
# trip data lands (ingest_trip_monthly.py, runs 6th/7th/8th of the month). It should
# be wired into Windows Task Scheduler the same way station_hourly_profile,
# station_daily_ridership, and station_daily_status already are (see
# CitibikeDailyStatusBuild in CLAUDE.md) — run this monthly, then run
# sync_ride_explorer_to_snowflake.py right after. Until that's set up, the Ride
# Explorer will silently go stale for any month added after the last manual run.
#
# Full TRUNCATE + rebuild each run (not incremental) — mirrors
# build_station_hourly_profile.py. At ~79 months of history this is a few minutes
# in Postgres, still far simpler and safer than incremental upsert logic.
#
# FIXED 2026-07-17: pre-2021 station_hourly_flow rows use legacy integer station
# IDs (e.g. '116'), which don't match station_information.station_id or
# .short_name directly. Without a fallback, lat/lon came back NULL for nearly
# all legacy rows (verified: 1 of 122,574 June 2019 rows had a non-null lat),
# and the web app API route filters out NULL-lat rows entirely, making the map
# look empty for any pre-2021 year. Added a join through station_id_crosswalk
# (legacy_id -> modern_uuid, built by data_historical/build_station_crosswalk.py)
# as a third fallback for lat/lon/name/capacity/borough.

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ride_explorer_profile (
    station_id             VARCHAR(50)  NOT NULL,
    year                    SMALLINT     NOT NULL,
    month                   SMALLINT     NOT NULL,
    day_of_week             SMALLINT     NOT NULL,
    hour_et                 SMALLINT     NOT NULL,
    station_name            VARCHAR(200),
    lat                     FLOAT,
    lon                     FLOAT,
    capacity                INTEGER,
    borough                 VARCHAR(50),
    total_departures        INTEGER,
    total_arrivals          INTEGER,
    total_member_trips      INTEGER,
    total_casual_trips      INTEGER,
    total_ebike_trips       INTEGER,
    total_classic_trips     INTEGER,
    hours_sampled           INTEGER,
    PRIMARY KEY (station_id, year, month, day_of_week, hour_et)
);
"""

TRUNCATE_SQL = "TRUNCATE TABLE ride_explorer_profile;"

INSERT_SQL = """
INSERT INTO ride_explorer_profile (
    station_id, year, month, day_of_week, hour_et,
    station_name, lat, lon, capacity, borough,
    total_departures, total_arrivals,
    total_member_trips, total_casual_trips,
    total_ebike_trips, total_classic_trips,
    hours_sampled
)
SELECT
    f.station_id,
    EXTRACT(YEAR  FROM f.hour AT TIME ZONE 'America/New_York')::SMALLINT AS year,
    EXTRACT(MONTH FROM f.hour AT TIME ZONE 'America/New_York')::SMALLINT AS month,
    EXTRACT(DOW   FROM f.hour AT TIME ZONE 'America/New_York')::SMALLINT AS day_of_week,
    EXTRACT(HOUR  FROM f.hour AT TIME ZONE 'America/New_York')::SMALLINT AS hour_et,
    COALESCE(si1.name,     si2.name,     si3.name)     AS station_name,
    COALESCE(si1.lat,      si2.lat,      cw.lat,  si3.lat) AS lat,
    COALESCE(si1.lon,      si2.lon,      cw.lon,  si3.lon) AS lon,
    COALESCE(si1.capacity, si2.capacity, si3.capacity) AS capacity,
    sb.borough,
    SUM(f.departures)      AS total_departures,
    SUM(f.arrivals)        AS total_arrivals,
    SUM(f.member_trips)    AS total_member_trips,
    SUM(f.casual_trips)    AS total_casual_trips,
    SUM(f.ebike_trips)     AS total_ebike_trips,
    SUM(f.classic_trips)   AS total_classic_trips,
    COUNT(*)               AS hours_sampled
FROM station_hourly_flow f
LEFT JOIN station_information si1
    ON si1.station_id = f.station_id
LEFT JOIN station_information si2
    ON si2.short_name = f.station_id
LEFT JOIN station_id_crosswalk cw
    ON cw.legacy_id = f.station_id
LEFT JOIN station_information si3
    ON si3.station_id = cw.modern_uuid
LEFT JOIN station_borough sb
    ON sb.station_id = COALESCE(si1.station_id, si2.station_id, cw.modern_uuid)
GROUP BY
    f.station_id,
    EXTRACT(YEAR  FROM f.hour AT TIME ZONE 'America/New_York'),
    EXTRACT(MONTH FROM f.hour AT TIME ZONE 'America/New_York'),
    EXTRACT(DOW   FROM f.hour AT TIME ZONE 'America/New_York'),
    EXTRACT(HOUR  FROM f.hour AT TIME ZONE 'America/New_York'),
    COALESCE(si1.name,     si2.name,     si3.name),
    COALESCE(si1.lat,      si2.lat,      cw.lat,  si3.lat),
    COALESCE(si1.lon,      si2.lon,      cw.lon,  si3.lon),
    COALESCE(si1.capacity, si2.capacity, si3.capacity),
    sb.borough
ON CONFLICT (station_id, year, month, day_of_week, hour_et) DO NOTHING;
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
        print("Aggregating station_hourly_flow -> ride_explorer_profile ...")
        cur.execute(INSERT_SQL)
        rows = cur.rowcount
    conn.commit()
    conn.close()
    print(f"Done. {rows:,} rows written to ride_explorer_profile.")
    print("Next: run sync_ride_explorer_to_snowflake.py to push this to Snowflake.")


if __name__ == "__main__":
    main()

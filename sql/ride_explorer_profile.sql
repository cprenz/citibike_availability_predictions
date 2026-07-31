-- Ride Explorer 3D map data source. Grain: (station_id, year, month, day_of_week, hour_et).
-- Pre-aggregated from station_hourly_flow (33M+ raw rows) so the web app never queries
-- raw trip data directly. Built by data_ingestion/build_ride_explorer_profile.py,
-- synced to Snowflake by data_ingestion/sync_ride_explorer_to_snowflake.py.
--
-- NOTE: this table is rebuilt manually today. It needs to become part of the automatic
-- monthly pipeline (see the note at the top of build_ride_explorer_profile.py) once new
-- trip CSVs land via ingest_trip_monthly.py, the same way station_hourly_profile,
-- station_daily_ridership, and station_daily_status already are.

CREATE TABLE IF NOT EXISTS ride_explorer_profile (
    station_id             VARCHAR(50)  NOT NULL,
    year                    SMALLINT     NOT NULL,
    month                   SMALLINT     NOT NULL,
    day_of_week             SMALLINT     NOT NULL,  -- 0=Sun ... 6=Sat, NYC local
    hour_et                 SMALLINT     NOT NULL,  -- 0-23, NYC local
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
    hours_sampled           INTEGER,  -- count of source rows summed, for computing AVG
    PRIMARY KEY (station_id, year, month, day_of_week, hour_et)
);

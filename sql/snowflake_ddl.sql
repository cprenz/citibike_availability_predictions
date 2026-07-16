-- Worksheet: Snowflake UI -> Projects -> Worksheets -> New Worksheet

CREATE DATABASE IF NOT EXISTS CITIBIKE;
USE DATABASE CITIBIKE;
CREATE SCHEMA IF NOT EXISTS PUBLIC;
USE SCHEMA PUBLIC;

-- ---------------------------------------------------------------------------
-- 1. station_daily_ridership
--    One row per station per day. Synced daily from local PostgreSQL.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS station_daily_ridership (
    station_id              VARCHAR(50)     NOT NULL,
    date                    DATE            NOT NULL,
    station_name            VARCHAR(200),
    borough                 VARCHAR(50),
    lat                     FLOAT,
    lon                     FLOAT,
    capacity                INTEGER,
    total_departures        INTEGER,
    total_arrivals          INTEGER,
    net_flow                INTEGER,
    ebike_departures        INTEGER,
    classic_departures      INTEGER,
    ebike_pct               FLOAT,
    classic_pct             FLOAT,
    member_trips            INTEGER,
    casual_trips            INTEGER,
    member_pct              FLOAT,
    casual_pct              FLOAT,
    avg_hourly_departures   FLOAT,
    PRIMARY KEY (station_id, date)
);

-- ---------------------------------------------------------------------------
-- 2. station_information
--    Station metadata (lat/lon, capacity, name). Synced daily.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS station_information (
    station_id      VARCHAR(255)    NOT NULL PRIMARY KEY,
    name            VARCHAR(200),
    short_name      VARCHAR(50),
    lat             FLOAT,
    lon             FLOAT,
    capacity        INTEGER,
    region_id       VARCHAR(20),
    last_updated    TIMESTAMP_TZ
);

-- ---------------------------------------------------------------------------
-- 4. station_daily_status
--    One row per station per day. Avg/min/max bikes, fill ratio, disabled.
--    Synced daily from local PostgreSQL.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS station_daily_status (
    station_id              VARCHAR(50)     NOT NULL,
    date                    DATE            NOT NULL,
    station_name            VARCHAR(200),
    borough                 VARCHAR(50),
    lat                     FLOAT,
    lon                     FLOAT,
    capacity                INTEGER,
    avg_bikes_available     FLOAT,
    min_bikes_available     INTEGER,
    max_bikes_available     INTEGER,
    avg_ebikes_available    FLOAT,
    avg_classic_available   FLOAT,
    avg_docks_available     FLOAT,
    avg_bikes_disabled      FLOAT,
    avg_fill_ratio          FLOAT,
    min_fill_ratio          FLOAT,
    max_fill_ratio          FLOAT,
    hours_sampled           INTEGER,
    PRIMARY KEY (station_id, date)
);

-- ---------------------------------------------------------------------------
-- 5. station_hourly_profile
--    Avg rides and availability by hour of day per station (~58k rows).
--    Full replace monthly after new trip CSVs land.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS station_hourly_profile (
    station_id              VARCHAR(50)     NOT NULL,
    station_name            VARCHAR(200),
    borough                 VARCHAR(50),
    lat                     FLOAT,
    lon                     FLOAT,
    capacity                INTEGER,
    hour_of_day             INTEGER         NOT NULL,
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

-- ---------------------------------------------------------------------------
-- 6. subscribers
--    One row per (contact, station, horizon). Written by the web app /api/subscribe.
--    Backed up nightly to local PostgreSQL via sync_subscribers_from_snowflake.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscribers (
    id                  INTEGER         AUTOINCREMENT PRIMARY KEY,
    email               VARCHAR(255),
    phone               VARCHAR(50),
    station_id          VARCHAR(255)    NOT NULL,
    horizon_minutes     INTEGER         NOT NULL,
    threshold           INTEGER,
    created_at          TIMESTAMP_TZ    DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT subscribers_contact_check CHECK (email IS NOT NULL OR phone IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- 3. model_predictions
--    One row per (station, predicted_at, horizon). Synced hourly.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_predictions (
    station_id              VARCHAR(50)     NOT NULL,
    predicted_at            TIMESTAMP_TZ    NOT NULL,
    horizon_minutes         INTEGER         NOT NULL,
    target_time             TIMESTAMP_TZ,
    predicted_value_lgbm    FLOAT,
    predicted_value_linear  FLOAT,
    pi_lower                FLOAT,
    pi_upper                FLOAT,
    predicted_prob_logistic FLOAT,
    actual_value            INTEGER,
    PRIMARY KEY (station_id, predicted_at, horizon_minutes)
);

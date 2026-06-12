-- ============================================================================
-- Citibike Availability Forecasting — Full Database Schema
-- ============================================================================
-- Database: PostgreSQL 16 + TimescaleDB
-- Run once against an empty `citibike` database to recreate the full data model.
-- All inserts in the pipeline use ON CONFLICT DO NOTHING, so this schema is the
-- single source of truth for table structure.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================================
-- LIVE GBFS ARCHIVE
-- ============================================================================

-- Static station metadata; upserted daily by ingest.py
CREATE TABLE IF NOT EXISTS station_information (
    station_id                     TEXT PRIMARY KEY,
    name                           TEXT,
    short_name                     TEXT,
    lat                            NUMERIC(10, 7),
    lon                            NUMERIC(10, 7),
    capacity                       INTEGER,
    region_id                      TEXT,
    station_type                   TEXT,
    has_kiosk                      BOOLEAN,
    electric_bike_surcharge_waiver BOOLEAN,
    eightd_has_key_dispenser       BOOLEAN,
    rental_methods                 TEXT[],
    rental_uris_ios                TEXT,
    rental_uris_android            TEXT,
    eightd_station_services        JSONB,
    external_id                    TEXT,
    last_updated                   TIMESTAMPTZ
);

-- Live bike availability snapshots archived every ~2.5 minutes.
-- Hypertable partitioned on fetched_at. No FK (hypertables disallow FKs).
CREATE TABLE IF NOT EXISTS station_status (
    fetched_at                TIMESTAMPTZ  NOT NULL,
    station_id                TEXT         NOT NULL,
    num_bikes_available       INTEGER,
    num_ebikes_available      INTEGER,
    num_bikes_disabled        INTEGER,
    num_docks_available       INTEGER,
    num_docks_disabled        INTEGER,
    num_scooters_available    INTEGER,
    num_scooters_unavailable  INTEGER,
    is_installed              SMALLINT,
    is_renting                SMALLINT,
    is_returning              SMALLINT,
    eightd_has_available_keys BOOLEAN,
    last_reported             BIGINT
);
SELECT create_hypertable('station_status', 'fetched_at', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_station_status_station_fetched
    ON station_status (station_id, fetched_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_station_status_unique
    ON station_status (station_id, fetched_at);

-- ============================================================================
-- HISTORICAL AVAILABILITY (Kaggle, 2016-2019 + 2021)
-- ============================================================================
-- 334.5M rows after dedup. Columns mirror station_status.
CREATE TABLE IF NOT EXISTS station_status_pre2021 (
    fetched_at                TIMESTAMPTZ  NOT NULL,
    station_id                TEXT         NOT NULL,
    num_bikes_available       INTEGER,
    num_ebikes_available      INTEGER,
    num_docks_available       INTEGER,
    num_bikes_disabled        INTEGER,
    num_docks_disabled        INTEGER,
    is_installed              SMALLINT,
    is_renting                SMALLINT,
    is_returning              SMALLINT,
    PRIMARY KEY (fetched_at, station_id)
);
SELECT create_hypertable('station_status_pre2021', 'fetched_at', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_status_pre2021_station_fetched
    ON station_status_pre2021 (station_id, fetched_at DESC);

-- ============================================================================
-- TRIP AGGREGATION TABLES
-- ============================================================================

-- Hourly departures/arrivals per station. 33M rows, 2019-2026 (excl. 2020).
CREATE TABLE IF NOT EXISTS station_hourly_flow (
    station_id      VARCHAR(50)  NOT NULL,
    hour            TIMESTAMPTZ  NOT NULL,
    departures      INTEGER      NOT NULL DEFAULT 0,
    arrivals        INTEGER      NOT NULL DEFAULT 0,
    member_trips    INTEGER      NOT NULL DEFAULT 0,
    casual_trips    INTEGER      NOT NULL DEFAULT 0,
    ebike_trips     INTEGER      NOT NULL DEFAULT 0,
    classic_trips   INTEGER      NOT NULL DEFAULT 0,
    PRIMARY KEY (station_id, hour)
);
SELECT create_hypertable('station_hourly_flow', 'hour', if_not_exists => TRUE);

-- Avg flow per station per (hour-of-day, day-of-week). Recomputed from flow.
CREATE TABLE IF NOT EXISTS station_demand_profile (
    station_id          VARCHAR(50)  NOT NULL,
    hour_of_day         SMALLINT     NOT NULL,
    day_of_week         SMALLINT     NOT NULL,
    avg_departures      DOUBLE PRECISION,
    avg_arrivals        DOUBLE PRECISION,
    avg_net_flow        DOUBLE PRECISION,
    PRIMARY KEY (station_id, hour_of_day, day_of_week)
);

-- Per-station summary features. One row per station. Recomputed from flow.
CREATE TABLE IF NOT EXISTS station_trip_features (
    station_id              VARCHAR(50)  PRIMARY KEY,
    member_ratio            DOUBLE PRECISION,
    ebike_ratio             DOUBLE PRECISION,
    avg_daily_departures    DOUBLE PRECISION,
    avg_daily_arrivals      DOUBLE PRECISION,
    station_role            VARCHAR(20),  -- source | sink | balanced
    computed_at             TIMESTAMPTZ
);

-- ============================================================================
-- MTA SUBWAY ENTRANCE TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS mta_subway_entrances (
    station_id                  INTEGER,
    complex_id                  INTEGER,
    gtfs_stop_id                VARCHAR(10),
    constituent_station_name    VARCHAR(100),
    daytime_routes              VARCHAR(50),
    line                        VARCHAR(50),
    division                    VARCHAR(10),
    borough                     VARCHAR(20),
    entrance_type               VARCHAR(50),
    entry_allowed               BOOLEAN,
    exit_allowed                BOOLEAN,
    entrance_latitude           DOUBLE PRECISION,
    entrance_longitude          DOUBLE PRECISION
);

-- Computed proximity features (one row per Citibike station).
CREATE TABLE IF NOT EXISTS citibike_station_subway_proximity (
    citibike_station_id         VARCHAR(50) PRIMARY KEY,
    nearest_entrance_dist_m     DOUBLE PRECISION,
    entrance_count_400m         INTEGER,
    entrance_count_800m         INTEGER,
    is_within_400m              BOOLEAN
);

-- ============================================================================
-- WEATHER TABLES
-- ============================================================================

-- Pre-2021 observed weather (ERA5). 2015-2020.
CREATE TABLE IF NOT EXISTS weather_pre2021_era5_observed (
    timestamp               TIMESTAMPTZ NOT NULL,
    temperature_2m          DOUBLE PRECISION,
    apparent_temperature    DOUBLE PRECISION,
    precipitation           DOUBLE PRECISION,
    rain                    DOUBLE PRECISION,
    snowfall                DOUBLE PRECISION,
    wind_speed_10m          DOUBLE PRECISION,
    wind_direction_10m      DOUBLE PRECISION,
    cloud_cover             DOUBLE PRECISION,
    relative_humidity_2m    DOUBLE PRECISION,
    dewpoint_2m             DOUBLE PRECISION,
    surface_pressure        DOUBLE PRECISION,
    PRIMARY KEY (timestamp)
);
SELECT create_hypertable('weather_pre2021_era5_observed', 'timestamp', if_not_exists => TRUE);

-- Pre-2021 archived forecast runs. 2018-2020.
CREATE TABLE IF NOT EXISTS weather_pre2021_gfs_forecast (
    run_time                TIMESTAMPTZ NOT NULL,
    valid_time              TIMESTAMPTZ NOT NULL,
    lead_time_hours         INTEGER,
    temperature_2m          DOUBLE PRECISION,
    apparent_temperature    DOUBLE PRECISION,
    precipitation           DOUBLE PRECISION,
    rain                    DOUBLE PRECISION,
    snowfall                DOUBLE PRECISION,
    wind_speed_10m          DOUBLE PRECISION,
    wind_direction_10m      DOUBLE PRECISION,
    cloud_cover             DOUBLE PRECISION,
    relative_humidity_2m    DOUBLE PRECISION,
    dewpoint_2m             DOUBLE PRECISION,
    surface_pressure        DOUBLE PRECISION,
    PRIMARY KEY (run_time, valid_time)
);
SELECT create_hypertable('weather_pre2021_gfs_forecast', 'valid_time', if_not_exists => TRUE);

-- Post-2021 observed weather (Open-Meteo archive). 2021-present, updated daily.
CREATE TABLE IF NOT EXISTS weather_post2021_openmeteo_observed (
    timestamp               TIMESTAMPTZ NOT NULL,
    temperature_2m          DOUBLE PRECISION,
    apparent_temperature    DOUBLE PRECISION,
    precipitation           DOUBLE PRECISION,
    rain                    DOUBLE PRECISION,
    snowfall                DOUBLE PRECISION,
    wind_speed_10m          DOUBLE PRECISION,
    wind_direction_10m      DOUBLE PRECISION,
    cloud_cover             DOUBLE PRECISION,
    relative_humidity_2m    DOUBLE PRECISION,
    dewpoint_2m             DOUBLE PRECISION,
    surface_pressure        DOUBLE PRECISION,
    PRIMARY KEY (timestamp)
);
SELECT create_hypertable('weather_post2021_openmeteo_observed', 'timestamp', if_not_exists => TRUE);

-- Post-2021 archived forecast runs (Open-Meteo). 2021-present, updated daily.
CREATE TABLE IF NOT EXISTS weather_post2021_openmeteo_forecast (
    run_time                TIMESTAMPTZ NOT NULL,
    valid_time              TIMESTAMPTZ NOT NULL,
    lead_time_hours         INTEGER,
    temperature_2m          DOUBLE PRECISION,
    apparent_temperature    DOUBLE PRECISION,
    precipitation           DOUBLE PRECISION,
    rain                    DOUBLE PRECISION,
    snowfall                DOUBLE PRECISION,
    wind_speed_10m          DOUBLE PRECISION,
    wind_direction_10m      DOUBLE PRECISION,
    cloud_cover             DOUBLE PRECISION,
    relative_humidity_2m    DOUBLE PRECISION,
    dewpoint_2m             DOUBLE PRECISION,
    surface_pressure        DOUBLE PRECISION,
    PRIMARY KEY (run_time, valid_time)
);
SELECT create_hypertable('weather_post2021_openmeteo_forecast', 'valid_time', if_not_exists => TRUE);

-- ============================================================================
-- INGESTION LOGS
-- ============================================================================

-- One row per GBFS poll: success | error | missed.
CREATE TABLE IF NOT EXISTS ingestion_log (
    id             SERIAL       PRIMARY KEY,
    logged_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    fetched_at     TIMESTAMPTZ,
    status         TEXT         NOT NULL,  -- 'success' | 'error' | 'missed'
    station_count  INTEGER,
    error_message  TEXT
);

-- One row per daily weather ingestion run: success | error | partial.
CREATE TABLE IF NOT EXISTS weather_ingestion_log (
    id              SERIAL PRIMARY KEY,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    script          TEXT NOT NULL,
    status          TEXT NOT NULL,  -- 'success' | 'error' | 'partial'
    rows_inserted   INTEGER,
    error_message   TEXT
);

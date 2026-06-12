-- Run this once against your citibike database to create all tables.
-- Requires TimescaleDB extension to be installed on the server.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Static station metadata; upserted daily by ingest.py
CREATE TABLE IF NOT EXISTS station_information (
    station_id                    TEXT PRIMARY KEY,
    name                          TEXT,
    short_name                    TEXT,
    lat                           NUMERIC(10, 7),
    lon                           NUMERIC(10, 7),
    capacity                      INTEGER,
    region_id                     TEXT,
    station_type                  TEXT,
    has_kiosk                     BOOLEAN,
    electric_bike_surcharge_waiver BOOLEAN,
    eightd_has_key_dispenser      BOOLEAN,
    rental_methods                TEXT[],
    rental_uris_ios               TEXT,
    rental_uris_android           TEXT,
    eightd_station_services       JSONB,
    external_id                   TEXT,
    last_updated                  TIMESTAMPTZ
);

-- Time-series station availability; partitioned by fetched_at via TimescaleDB.
-- No FK to station_information: TimescaleDB hypertables do not support FK constraints.
-- Referential integrity is maintained by ingest.py, which always upserts
-- station_information before inserting station_status rows.
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

-- TimescaleDB auto-indexes fetched_at. This composite index covers the most
-- common query: "give me the last N readings for station X".
CREATE INDEX IF NOT EXISTS idx_station_status_station_fetched
    ON station_status (station_id, fetched_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_station_status_unique
    ON station_status (station_id, fetched_at);

-- Tracks every script run: success, error, or missed poll window.
CREATE TABLE IF NOT EXISTS ingestion_log (
    id             SERIAL       PRIMARY KEY,
    logged_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    fetched_at     TIMESTAMPTZ,
    status         TEXT         NOT NULL,  -- 'success' | 'error' | 'missed'
    station_count  INTEGER,
    error_message  TEXT
);

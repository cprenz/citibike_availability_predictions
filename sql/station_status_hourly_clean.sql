-- station_status_hourly_clean — the cleaned, hourly-sampled availability table.
--
-- This is the INTERMEDIATE table between the raw archive and training_features:
--
--     station_status / station_status_pre2021   (raw, ~334M rows, read-only)
--             |  clean + point-sample to 1 row/station/hour   (pandas, ONCE)
--             v
--     station_status_hourly_clean                (THIS TABLE — already clean)
--             |  build lags / weather / targets   (feature build)
--             v
--     training_features
--
-- Grain: one row per (station_id, hour). Availability is a STOCK, so we
-- POINT-SAMPLE one snapshot per hour (the snapshot at/closest to the end of the
-- hour) — we do NOT sum the ~2.5-min polls within the hour.
--
-- Cleaning happens in pandas BEFORE the COPY into this table
-- (model_training/build_clean_availability.py). By the time rows land here they
-- are already scrubbed: no dupes, no impossible values, booleans coerced, etc.
-- Nothing downstream re-cleans — feature building just reads this table.

CREATE TABLE IF NOT EXISTS station_status_hourly_clean (
    station_id              VARCHAR(50)  NOT NULL,
    hour                    TIMESTAMPTZ  NOT NULL,   -- truncated to the hour, UTC

    num_bikes_available     INTEGER,
    num_ebikes_available    INTEGER,
    num_docks_available     INTEGER,
    num_bikes_disabled      INTEGER,
    num_docks_disabled      INTEGER,

    is_installed            BOOLEAN,
    is_renting              BOOLEAN,
    is_returning            BOOLEAN,

    -- capacity frozen at clean time (from station_information) so fill_ratio and
    -- range checks have a denominator without re-joining during feature build.
    capacity                INTEGER,

    PRIMARY KEY (station_id, hour)
);

-- TimescaleDB hypertable partitioned on the sampled hour.
SELECT create_hypertable('station_status_hourly_clean', 'hour', if_not_exists => TRUE);

-- Per-station time-series lookups during the feature build (lag windows).
CREATE INDEX IF NOT EXISTS idx_status_hourly_clean_station_hour
    ON station_status_hourly_clean (station_id, hour DESC);

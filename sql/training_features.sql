-- training_features — the single, model-ready dataset for Phase 3.
--
-- Grain: one row per (station_id, timestamp, horizon_minutes).
--   timestamp       = the "as-of" moment a prediction is made
--   horizon_minutes = how far ahead we predict (10, 60, 180, 360, 720, 1440, 2880)
--
-- All features must be computable from data with timestamp <= `timestamp`
-- (no future leakage). The ONLY forward look is the two target columns.
--
-- Populated in batches by model_training/build_training_features.py via
-- INSERT ... SELECT run inside the database (never pulled into pandas).

CREATE TABLE IF NOT EXISTS training_features (
    -- ---- Identifiers ----
    station_id                              VARCHAR(50)  NOT NULL,
    "timestamp"                             TIMESTAMPTZ  NOT NULL,
    horizon_minutes                         INTEGER      NOT NULL,

    -- ---- Targets (the only columns that look into the future) ----
    bikes_available_at_horizon              INTEGER,      -- regression target
    bike_available_binary                   SMALLINT,     -- classification target (1 if >0 bikes)

    -- ---- Time features (derived from timestamp) ----
    hour_of_day                             SMALLINT,
    day_of_week                             SMALLINT,
    month                                   SMALLINT,
    season                                  VARCHAR(10),
    is_weekend                              BOOLEAN,
    is_holiday                              BOOLEAN,

    -- ---- Current availability (snapshot at timestamp) ----
    num_bikes_available                     INTEGER,
    num_ebikes_available                    INTEGER,
    num_docks_available                     INTEGER,
    num_bikes_disabled                      INTEGER,

    -- ---- Normalized / relative availability (capacity-normalized, stationary,
    --      comparable across stations of different sizes) ----
    fill_ratio                              DOUBLE PRECISION,  -- bikes / capacity
    fill_ratio_change_1hr                   DOUBLE PRECISION,  -- (bikes - bikes_1hr_ago) / capacity
    rolling_mean_fill_ratio_6hr             DOUBLE PRECISION,  -- avg fill_ratio, last 6h

    -- ---- Lag features (backward windows over the status archive) ----
    bikes_1hr_ago                           INTEGER,
    bikes_3hr_ago                           INTEGER,
    bikes_6hr_ago                           INTEGER,
    bikes_12hr_ago                          INTEGER,
    bikes_same_hour_yesterday               INTEGER,
    bikes_same_hour_same_weekday_4wk_avg    DOUBLE PRECISION,
    rate_of_change_10min                    DOUBLE PRECISION,
    rate_of_change_20min                    DOUBLE PRECISION,
    rate_of_change_30min                    DOUBLE PRECISION,
    emptying_frequency                      DOUBLE PRECISION,
    capping_frequency                       DOUBLE PRECISION,
    rebalancing_signal                      DOUBLE PRECISION,
    time_since_last_rebalancing             DOUBLE PRECISION,

    -- ---- Observed weather (as-of timestamp) ----
    temperature_2m                          DOUBLE PRECISION,
    apparent_temperature                    DOUBLE PRECISION,
    precipitation                           DOUBLE PRECISION,
    rain                                    DOUBLE PRECISION,
    snowfall                                DOUBLE PRECISION,
    wind_speed_10m                          DOUBLE PRECISION,
    cloud_cover                             DOUBLE PRECISION,
    relative_humidity_2m                    DOUBLE PRECISION,

    -- ---- Forecast weather (matched by lead time to the horizon) ----
    forecast_temperature_2m                 DOUBLE PRECISION,
    forecast_apparent_temperature           DOUBLE PRECISION,
    forecast_precipitation                  DOUBLE PRECISION,
    forecast_rain                           DOUBLE PRECISION,
    forecast_snowfall                       DOUBLE PRECISION,
    forecast_wind_speed_10m                 DOUBLE PRECISION,
    forecast_cloud_cover                    DOUBLE PRECISION,
    forecast_relative_humidity_2m           DOUBLE PRECISION,

    -- ---- Station static (joined by station_id) ----
    capacity                                INTEGER,
    nearest_entrance_dist_m                 DOUBLE PRECISION,
    entrance_count_400m                     INTEGER,
    entrance_count_800m                     INTEGER,
    is_within_400m                          BOOLEAN,
    member_ratio                            DOUBLE PRECISION,
    ebike_ratio                             DOUBLE PRECISION,
    station_role                            VARCHAR(20),

    -- ---- Demand signals (trip aggregates) ----
    departures_this_hour                    INTEGER,
    arrivals_this_hour                      INTEGER,
    avg_departures_this_hour_dow            DOUBLE PRECISION,
    avg_arrivals_this_hour_dow              DOUBLE PRECISION,
    avg_net_flow_this_hour_dow              DOUBLE PRECISION,

    -- ---- Neighbor features ----
    avg_availability_5_nearest_stations     DOUBLE PRECISION,

    PRIMARY KEY (station_id, "timestamp", horizon_minutes)
);

-- TimescaleDB hypertable partitioned on the as-of timestamp.
SELECT create_hypertable('training_features', 'timestamp', if_not_exists => TRUE);

-- Pull pattern for Phase 3: SELECT ... WHERE horizon_minutes = ? AND timestamp < test_cutoff
CREATE INDEX IF NOT EXISTS idx_training_features_horizon_ts
    ON training_features (horizon_minutes, "timestamp");

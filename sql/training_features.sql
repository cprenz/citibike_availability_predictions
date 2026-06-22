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
    num_ebikes_was_null                     BOOLEAN NOT NULL DEFAULT FALSE,
    num_bikes_disabled_was_null             BOOLEAN NOT NULL DEFAULT FALSE,

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

    -- ---- Count-change features (absolute deltas vs lag; complements the
    --      capacity-normalized fill_ratio_change_1hr above). GBFS
    --      num_bikes_available is the TOTAL and INCLUDES ebikes, so classic
    --      (= total - ebikes) is tracked separately to avoid the total/ebike
    --      overlap. NULL wherever the matching lag row is missing. ----
    change_bikes_1hr                        INTEGER,
    change_bikes_3hr                        INTEGER,
    change_bikes_6hr                        INTEGER,
    change_bikes_12hr                       INTEGER,
    change_ebikes_1hr                       INTEGER,
    change_ebikes_3hr                       INTEGER,
    change_ebikes_6hr                       INTEGER,
    change_ebikes_12hr                      INTEGER,
    change_classic_1hr                      INTEGER,
    change_classic_3hr                      INTEGER,
    change_classic_6hr                      INTEGER,
    change_classic_12hr                     INTEGER,

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

    -- ---- Time features — cyclical encodings (pure transforms; fixes hour-23/hour-0
    --      discontinuity; helps linear models substantially, harmless for XGBoost) ----
    hour_sin                                DOUBLE PRECISION,
    hour_cos                                DOUBLE PRECISION,
    dow_sin                                 DOUBLE PRECISION,
    dow_cos                                 DOUBLE PRECISION,
    month_sin                               DOUBLE PRECISION,
    month_cos                               DOUBLE PRECISION,

    -- ---- Demand signals (trip aggregates) ----
    departures_this_hour                    INTEGER,
    arrivals_this_hour                      INTEGER,
    avg_departures_this_hour_dow            DOUBLE PRECISION,
    avg_arrivals_this_hour_dow              DOUBLE PRECISION,
    avg_net_flow_this_hour_dow              DOUBLE PRECISION,

    -- Cumulative expected net flow: sum of avg_net_flow_this_hour_dow across the
    -- next H hours from station_demand_profile. Horizon-specific demand climatology
    -- approximating bikes_at_horizon ≈ bikes_now + net_flow_over_window.
    cumulative_expected_net_flow_1hr        DOUBLE PRECISION,
    cumulative_expected_net_flow_3hr        DOUBLE PRECISION,
    cumulative_expected_net_flow_6hr        DOUBLE PRECISION,
    cumulative_expected_net_flow_12hr       DOUBLE PRECISION,
    cumulative_expected_net_flow_24hr       DOUBLE PRECISION,

    -- Recent net-flow momentum lags: arrivals-departures from station_hourly_flow
    -- lagged 1/3/6 hours. NULL pre-2019 and for JC stations (no trip CSV data).
    net_flow_1hr                            DOUBLE PRECISION,
    net_flow_3hr                            DOUBLE PRECISION,
    net_flow_6hr                            DOUBLE PRECISION,

    -- ---- Neighbor features ----
    avg_availability_5_nearest_stations     DOUBLE PRECISION,

    PRIMARY KEY (station_id, "timestamp", horizon_minutes)
);

-- TimescaleDB hypertable partitioned on the as-of timestamp.
SELECT create_hypertable('training_features', 'timestamp', if_not_exists => TRUE);

-- Pull pattern for Phase 3: SELECT ... WHERE horizon_minutes = ? AND timestamp < test_cutoff
CREATE INDEX IF NOT EXISTS idx_training_features_horizon_ts
    ON training_features (horizon_minutes, "timestamp");

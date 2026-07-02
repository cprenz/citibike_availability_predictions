-- model_predictions — stores hourly scored predictions for all active stations
-- across all 6 forecast horizons.
--
-- The scoring script runs hourly via Task Scheduler:
--   1. Loads all 18 .joblib artifacts (6 LightGBM + 6 Linear + 6 Logistic)
--   2. Pulls current GBFS + weather from PostgreSQL
--   3. Builds one feature row per active station x 6 horizons (~14,400 rows)
--   4. Scores all rows and writes results here
--
-- One row per (station, predicted_at, horizon) holds all 3 models' outputs in
-- separate columns, rather than one row per model — cheaper to query for the
-- web app ("give me every model's take on this station/horizon" is one row,
-- no join/pivot needed).
--
-- target_time = predicted_at + horizon_minutes, stored directly at write time
-- so consumers don't have to recompute it.
--
-- actual_value starts NULL and gets backfilled once the horizon passes and the
-- real GBFS reading arrives. This enables predicted vs actual analysis in
-- Tableau and the autoregressive error correction feature post-deployment.

CREATE TABLE IF NOT EXISTS model_predictions (
    station_id               VARCHAR(50)      NOT NULL,
    predicted_at              TIMESTAMPTZ      NOT NULL,  -- when the prediction was made
    horizon_minutes           INTEGER          NOT NULL,  -- 60, 180, 360, 720, 1440, 2880
    target_time                TIMESTAMPTZ      NOT NULL, -- predicted_at + horizon_minutes
    predicted_value_lgbm       DOUBLE PRECISION,           -- LightGBM bike count (primary regressor)
    predicted_value_linear     DOUBLE PRECISION,           -- Linear bike count (interpretable regressor)
    pi_lower                   DOUBLE PRECISION,           -- Linear 95% PI lower bound (clipped at 0)
    pi_upper                   DOUBLE PRECISION,           -- Linear 95% PI upper bound
    predicted_prob_logistic    DOUBLE PRECISION,           -- Logistic P(bike available)
    actual_value                INTEGER,                    -- filled in after horizon passes
    PRIMARY KEY (station_id, predicted_at, horizon_minutes)
);

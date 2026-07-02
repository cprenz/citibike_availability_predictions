-- One row per model per horizon per scoring run (18 rows per run:
-- 6 horizons x 3 models). Allows filtering by model_type to detect
-- silent failures per model independently across horizons.

CREATE TABLE IF NOT EXISTS scoring_log (
    id                  SERIAL PRIMARY KEY,
    scored_at           TIMESTAMPTZ NOT NULL,
    horizon_minutes     INTEGER NOT NULL,
    model_type          VARCHAR(20) NOT NULL,
    predictions_written INTEGER,
    duration_seconds    DOUBLE PRECISION,
    status              TEXT NOT NULL,
    error_message       TEXT
);

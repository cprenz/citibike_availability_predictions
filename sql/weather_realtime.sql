-- Near-realtime observed weather, populated hourly from Open-Meteo forecast API
-- with past_hours=6. Separate from weather_post2021_openmeteo_observed (ERA5,
-- ~5 day lag) so the two sources never collide. score_stations.py queries this
-- table first for current observed weather, falls back to ERA5 if empty.
-- ON CONFLICT DO UPDATE so refined model values overwrite earlier estimates.

CREATE TABLE IF NOT EXISTS weather_realtime (
    timestamp               TIMESTAMPTZ NOT NULL PRIMARY KEY,
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
    surface_pressure        DOUBLE PRECISION
);

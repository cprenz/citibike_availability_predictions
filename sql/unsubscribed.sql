-- Churn tracking table. When a subscriber clicks "unsubscribe", their row is
-- copied here before being deleted from subscribers. Keeps the full history
-- of who left and when, without polluting the active subscribers table.
--
-- One row per (email, station_id) per unsubscribe event. If the same person
-- re-subscribes and unsubscribes again, a second row is written.
--
-- Run once in Snowflake:

CREATE TABLE IF NOT EXISTS unsubscribed (
    id              INTEGER AUTOINCREMENT PRIMARY KEY,
    email           TEXT,
    station_id      TEXT,
    station_name    TEXT,
    target_time     VARCHAR(5),
    subscribed_at   TIMESTAMP_TZ,
    unsubscribed_at TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP
);

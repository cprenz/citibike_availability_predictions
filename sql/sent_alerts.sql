-- Deduplication table for send_alerts.py.
-- One row per (subscriber_id, alert_date) prevents re-sending the same alert
-- on the same calendar day.
--
-- Snowflake PRIMARY KEY is informational only (not enforced at write time),
-- so send_alerts.py checks before inserting rather than relying on a DB error.
--
-- Run once in Snowflake:
--   CREATE TABLE sent_alerts (
--       subscriber_id  INTEGER      NOT NULL,
--       alert_date     DATE         NOT NULL,
--       sent_at        TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP
--   );
--
-- send_alerts.py also calls CREATE TABLE IF NOT EXISTS at startup, so this
-- file is documentation / a manual fallback, not required for normal operation.

CREATE TABLE IF NOT EXISTS sent_alerts (
    subscriber_id  INTEGER      NOT NULL,
    alert_date     DATE         NOT NULL,
    sent_at        TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP
);

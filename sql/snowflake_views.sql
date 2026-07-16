-- Run this entire file in Snowflake UI:
--   Projects -> Worksheets -> New Worksheet -> paste -> Run All

USE DATABASE CITIBIKE;
USE SCHEMA PUBLIC;

-- ---------------------------------------------------------------------------
-- 1. citibike_station_monthly
--    Monthly per-station ridership. ~130k rows (4,514 stations x ~89 months).
--    Used for per-station trend charts in Tableau.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW citibike_station_monthly AS
SELECT
    station_id,
    station_name,
    borough,
    lat,
    lon,
    capacity,
    DATE_TRUNC('month', date)            AS month,
    SUM(total_departures)                AS total_rides,
    SUM(total_arrivals)                  AS total_arrivals,
    SUM(ebike_departures)                AS ebike_rides,
    SUM(classic_departures)              AS classic_rides,
    SUM(member_trips)                    AS member_rides,
    SUM(casual_trips)                    AS casual_rides,
    ROUND(AVG(ebike_pct), 4)             AS avg_ebike_pct,
    ROUND(AVG(classic_pct), 4)           AS avg_classic_pct,
    ROUND(AVG(member_pct), 4)            AS avg_member_pct,
    ROUND(AVG(casual_pct), 4)            AS avg_casual_pct,
    ROUND(AVG(avg_hourly_departures), 4) AS avg_hourly_departures
FROM station_daily_ridership
GROUP BY station_id, station_name, borough, lat, lon, capacity,
         DATE_TRUNC('month', date);


-- ---------------------------------------------------------------------------
-- 2. citibike_daily_totals
--    Daily citywide totals by borough. ~550 rows.
--    Used for the overall rides-over-time chart (matches ctbk.dev main chart).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW citibike_daily_totals AS
SELECT
    date,
    borough,
    SUM(total_departures)   AS total_rides,
    SUM(total_arrivals)     AS total_arrivals,
    SUM(ebike_departures)   AS ebike_rides,
    SUM(classic_departures) AS classic_rides,
    SUM(member_trips)       AS member_rides,
    SUM(casual_trips)       AS casual_rides
FROM station_daily_ridership
GROUP BY date, borough
ORDER BY date, borough;


-- ---------------------------------------------------------------------------
-- 3. citibike_station_summary
--    One row per station — total rides across all years, map metrics.
--    ~2,400 rows. Used for the station map and top-stations ranked bar chart.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW citibike_station_summary AS
SELECT
    r.station_id,
    r.station_name,
    r.borough,
    r.lat,
    r.lon,
    r.capacity,
    SUM(r.total_departures)             AS total_rides,
    SUM(r.ebike_departures)             AS total_ebike_rides,
    SUM(r.classic_departures)           AS total_classic_rides,
    ROUND(AVG(r.member_pct), 4)         AS avg_member_pct,
    ROUND(AVG(r.ebike_pct), 4)          AS avg_ebike_pct,
    ROUND(AVG(r.avg_hourly_departures), 4) AS avg_hourly_departures
FROM station_daily_ridership r
GROUP BY r.station_id, r.station_name, r.borough, r.lat, r.lon, r.capacity;


-- ---------------------------------------------------------------------------
-- 4. citibike_hourly_profile
--    Average rides by hour of day across all stations. 24 rows.
--    Used for the hour-of-day chart.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW citibike_hourly_profile AS
SELECT
    hour_of_day,
    ROUND(AVG(avg_departures), 4)           AS avg_departures,
    ROUND(AVG(avg_arrivals), 4)             AS avg_arrivals,
    ROUND(AVG(avg_net_flow), 4)             AS avg_net_flow,
    ROUND(AVG(avg_ebike_departures), 4)     AS avg_ebike_departures,
    ROUND(AVG(avg_classic_departures), 4)   AS avg_classic_departures,
    ROUND(AVG(avg_bikes_available), 4)      AS avg_bikes_available,
    ROUND(AVG(avg_fill_ratio), 4)           AS avg_fill_ratio
FROM station_hourly_profile
GROUP BY hour_of_day
ORDER BY hour_of_day;


-- ---------------------------------------------------------------------------
-- 5. citibike_station_monthly_status
--    Monthly per-station availability metrics. ~47k rows.
--    Used for fill ratio trends and availability maps.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW citibike_station_monthly_status AS
SELECT
    station_id,
    station_name,
    borough,
    lat,
    lon,
    capacity,
    DATE_TRUNC('month', date)               AS month,
    ROUND(AVG(avg_bikes_available), 4)      AS avg_bikes_available,
    ROUND(AVG(avg_ebikes_available), 4)     AS avg_ebikes_available,
    ROUND(AVG(avg_classic_available), 4)    AS avg_classic_available,
    ROUND(AVG(avg_fill_ratio), 4)           AS avg_fill_ratio,
    ROUND(MIN(min_fill_ratio), 4)           AS min_fill_ratio,
    ROUND(MAX(max_fill_ratio), 4)           AS max_fill_ratio,
    ROUND(AVG(avg_bikes_disabled), 4)       AS avg_bikes_disabled,
    SUM(hours_sampled)                      AS hours_sampled
FROM station_daily_status
GROUP BY station_id, station_name, borough, lat, lon, capacity,
         DATE_TRUNC('month', date);

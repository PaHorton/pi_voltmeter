-- TimescaleDB setup for the voltmeter. Run once as the admin user (tsdbadmin):
--   psql "postgres://tsdbadmin@HOST:PORT/tsdb?sslmode=require" -f db/timescale_setup.sql
-- Idempotent: safe to run again. Needs the `readings` hypertable the Pi sink already created.

-- 1-minute rollup that TimescaleDB keeps up to date. The dashboard reads it for ranges >= 6 h
-- (set READINGS_AGG=readings_1m). Grouped by formula so a formula change mid-range stays correct.
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', time) AS bucket,
       device_id, channel, formula,
       avg(volts) AS volts,
       count(*)   AS n
FROM readings
GROUP BY 1, 2, 3, 4
WITH NO DATA;

-- Include not-yet-materialized rows so the newest minute is always present.
ALTER MATERIALIZED VIEW readings_1m SET (timescaledb.materialized_only = false);

SELECT add_continuous_aggregate_policy('readings_1m',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE);

-- Backfill what is already there.
CALL refresh_continuous_aggregate('readings_1m', NULL, now() - INTERVAL '1 minute');

-- Compress raw rows older than a day.
ALTER TABLE readings SET (timescaledb.compress,
                          timescaledb.compress_segmentby = 'device_id, channel',
                          timescaledb.compress_orderby   = 'time DESC');
SELECT add_compression_policy('readings', INTERVAL '1 day', if_not_exists => TRUE);

-- Keep 90 days of raw rows (the 1-minute rollup is kept). Delete this line to keep everything.
SELECT add_retention_policy('readings', INTERVAL '90 days', if_not_exists => TRUE);

-- The web role may read the rollup too.
GRANT SELECT ON readings_1m TO voltmeter_web;

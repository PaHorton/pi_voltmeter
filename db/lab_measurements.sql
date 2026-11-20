-- Run explicitly as a database administrator, after installing/enabling TimescaleDB:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/lab_measurements.sql
-- This creates a separate hypertable; it does not alter legacy readings or policies.
CREATE TABLE IF NOT EXISTS public.lab_measurements (
    time timestamptz NOT NULL,
    logger_id text NOT NULL,
    system_id text NOT NULL,
    instrument_id text NOT NULL,
    sensor_id text NOT NULL,
    native_value double precision,
    native_unit text NOT NULL,
    value double precision,
    unit text NOT NULL,
    quality text NOT NULL CHECK (quality IN ('good', 'invalid', 'unavailable')),
    conversion text,
    PRIMARY KEY (time, logger_id, system_id, instrument_id, sensor_id),
    CHECK ((quality = 'good' AND value IS NOT NULL)
        OR (quality IN ('invalid', 'unavailable') AND value IS NULL))
);

SELECT create_hypertable('public.lab_measurements', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS lab_measurements_system_time_idx
    ON public.lab_measurements (system_id, time DESC);
CREATE INDEX IF NOT EXISTS lab_measurements_system_sensor_time_idx
    ON public.lab_measurements (system_id, sensor_id, time DESC);

-- After creating the appropriate roles separately, an administrator can grant:
-- GRANT USAGE ON SCHEMA public TO your_writer_role;
-- GRANT INSERT ON public.lab_measurements TO your_writer_role;
-- GRANT SELECT (time, logger_id, system_id, instrument_id, sensor_id)
--     ON public.lab_measurements TO your_writer_role;
-- GRANT USAGE ON SCHEMA public TO your_reader_role;
-- GRANT SELECT ON public.lab_measurements TO your_reader_role;
-- Replace role names with actual, properly quoted identifiers. No role is assumed.

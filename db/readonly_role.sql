-- Run once as the admin user (tsdbadmin) in psql. Replace the password.
-- The web app uses this role: it can read readings and nothing else.
CREATE ROLE voltmeter_web LOGIN PASSWORD 'CHANGE_ME';
GRANT USAGE ON SCHEMA public TO voltmeter_web;
GRANT SELECT ON readings TO voltmeter_web;

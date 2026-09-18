-- Creates a dedicated PostgreSQL role for the AI Reports feature (STORA
-- reports app, "AI mode") -- genuinely read-only at the database level,
-- not just "the AI was told not to write". Run this once, as a superuser
-- (the same account used to create stora_usr -- see DEPLOYMENT.md), against
-- the stora_db database:
--
--   sudo -u postgres psql -d stora_db -f db_create_ai_readonly_role.sql
--
-- If that still asks for a password (some setups have `local all postgres`
-- set to md5 in pg_hba.conf instead of the usual peer auth -- check
-- /var/log/postgresql/postgresql-*-main.log for "Connection matched file
-- ... md5" to confirm), temporarily switch that one line to `peer`,
-- `systemctl restart postgresql`, run this script, then switch it back and
-- restart again.
--
-- The password below (stora_psswd) matches DB_PASSWORD's own dev-only
-- fallback in STORA/settings.py -- fine for local dev/single-pilot-machine
-- use (same trust level as the app's own DB user already has). Change it
-- to something else here AND in DB_READONLY_PASSWORD in .env if that's not
-- good enough for a given machine.

CREATE ROLE stora_ai_readonly WITH LOGIN PASSWORD 'stora_psswd';

-- Can connect to the database and see the schema, but starts with zero
-- table access until the GRANT below.
GRANT CONNECT ON DATABASE stora_db TO stora_ai_readonly;
GRANT USAGE ON SCHEMA public TO stora_ai_readonly;

-- Read access to every table that exists right now...
GRANT SELECT ON ALL TABLES IN SCHEMA public TO stora_ai_readonly;

-- ...and to every table created by stora_usr from now on (new migrations),
-- without needing to re-run this GRANT after each one.
ALTER DEFAULT PRIVILEGES FOR ROLE stora_usr IN SCHEMA public
    GRANT SELECT ON TABLES TO stora_ai_readonly;

-- Explicit belt-and-suspenders: even though SELECT-only was never combined
-- with any write grant above, make it impossible for a future GRANT
-- mistake to quietly hand this role write access -- every session opened
-- as this role refuses to write, full stop, at the Postgres session level,
-- regardless of what table-level grants it ends up with.
ALTER ROLE stora_ai_readonly SET default_transaction_read_only = on;

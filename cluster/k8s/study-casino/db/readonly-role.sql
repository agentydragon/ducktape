-- Idempotent provisioning of the read-only role used by sandbox agents.
-- Re-runs of the provisioner Job ALTER the password to match the current
-- `study-casino-db-readonly` Secret.
DO $$ BEGIN
  CREATE ROLE study_casino_ro LOGIN PASSWORD :'ro_pw';
EXCEPTION WHEN duplicate_object THEN
  ALTER ROLE study_casino_ro PASSWORD :'ro_pw';
END $$;

GRANT CONNECT ON DATABASE studycasino TO study_casino_ro;
GRANT USAGE ON SCHEMA public TO study_casino_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO study_casino_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO study_casino_ro;

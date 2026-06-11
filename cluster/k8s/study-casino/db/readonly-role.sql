-- Object-level GRANTs for the CNPG-managed `study_casino_ro` role.
-- Role creation + password sync live on the CNPG Cluster spec under
-- `managed.roles[name=study_casino_ro]`. This script only grants object
-- permissions (CNPG does not manage these) and runs as the `studycasino`
-- owner, which has all privileges on its own database and tables.
-- ALTER DEFAULT PRIVILEGES covers future Alembic-created audit tables such
-- as rng_action_audits/rng_call_audits even if this Job ran before the app
-- applied those migrations.
GRANT CONNECT ON DATABASE studycasino TO study_casino_ro;
GRANT USAGE ON SCHEMA public TO study_casino_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO study_casino_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO study_casino_ro;

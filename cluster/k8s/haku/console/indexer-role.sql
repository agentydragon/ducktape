-- Object-level GRANTs for the CNPG-managed `haku_indexer` role (the haku-indexer
-- worker). Role creation + password sync live on the CNPG Cluster spec under
-- `managed.roles[name=haku_indexer]` (db/postgres-cluster.yaml). This script only
-- grants object permissions (CNPG does not manage these) and runs as the
-- `approval_store` owner, which has all privileges on its own database and tables.
--
-- The recall_index schema is created by the migration Job, which shares this
-- Kustomization with no ordering between them, so the provisioner Job running this
-- script retries until the schema exists rather than failing a fresh bootstrap.
--
-- The grants are deliberately the worker's whole authority: recall-index read/write.
-- No approval-ledger, identity, credential, or OAuth table is readable.
-- ALTER DEFAULT PRIVILEGES covers recall tables a future migration adds.
GRANT CONNECT ON DATABASE approval_store TO haku_indexer;
GRANT USAGE ON SCHEMA recall_index TO haku_indexer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA recall_index TO haku_indexer;
ALTER DEFAULT PRIVILEGES IN SCHEMA recall_index GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO haku_indexer;

-- Object-level GRANTs for the CNPG-managed `haku_indexer` role (the haku-indexer
-- worker). Role creation + password sync live on the CNPG Cluster spec under
-- `managed.roles[name=haku_indexer]` (db/postgres-cluster.yaml). This script only
-- grants object permissions (CNPG does not manage these) and runs as the
-- `approval_store` owner, which has all privileges on its own database and tables.
--
-- The Job lives in this app-layer Kustomization, not db/, because the recall_index
-- schema is created by the migration Job (this Kustomization's dependency) — the
-- grants would fail on a fresh bootstrap if applied before it.
--
-- The grants are deliberately the worker's whole authority: recall-index read/write
-- plus read-only chat-source access. No approval-ledger, identity, credential, or
-- OAuth table is readable. ALTER DEFAULT PRIVILEGES covers recall tables a future
-- migration adds; the chat-source grant is a single table on purpose, so a new
-- console table never leaks to the worker by default. A migration that drops and
-- recreates conversation_item also drops this grant — the worker's chat sweep then
-- fails loudly until this script is re-run (edit it to re-trigger the Job).
GRANT CONNECT ON DATABASE approval_store TO haku_indexer;
GRANT USAGE ON SCHEMA recall_index TO haku_indexer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA recall_index TO haku_indexer;
ALTER DEFAULT PRIVILEGES IN SCHEMA recall_index GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO haku_indexer;
GRANT USAGE ON SCHEMA public TO haku_indexer;
GRANT SELECT ON public.conversation_item TO haku_indexer;

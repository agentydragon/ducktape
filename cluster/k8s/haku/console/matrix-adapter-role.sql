-- Object-level GRANTs for the CNPG-managed `haku_matrix_adapter` role (the
-- haku-matrix-adapter worker). Role creation + password sync live on the CNPG Cluster
-- spec under `managed.roles[name=haku_matrix_adapter]` (db/postgres-cluster.yaml). This
-- script only grants object permissions (CNPG does not manage these) and runs as the
-- `approval_store` owner, which has all privileges on its own database and tables.
--
-- The grants are deliberately the worker's whole authority, in three tiers. No
-- approval-ledger, OAuth, push, grant, or session-frame table is readable, and a new
-- console table never leaks to the worker by default: the touched tables are enumerated
-- on purpose, with no default-privileges blanket. A migration that drops and recreates
-- one of them also drops its grant — the worker then fails loudly until this script is
-- re-run (edit it to re-trigger the Job).
GRANT CONNECT ON DATABASE approval_store TO haku_matrix_adapter;
GRANT USAGE ON SCHEMA public TO haku_matrix_adapter;

-- 1. The channel's own state: full ownership.
GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.matrix_access_token,
  public.matrix_sync_watermark,
  public.matrix_revision,
  public.matrix_room_copy,
  public.matrix_outbox,
  public.matrix_ingress_event,
  public.channel_attachment,
  public.channel_cursor
TO haku_matrix_adapter;

-- 2. The conversation seam: the record it subscribes to and appends authored facts to, and
-- the offer-input path (which stamps the serving session's updated_at and checks turn/prompt
-- state — UPDATE on `conversation` is also what its row lock requires).
GRANT SELECT, INSERT, UPDATE ON public.conversation TO haku_matrix_adapter;
GRANT SELECT, INSERT ON public.conversation_event, public.conversation_item, public.conversation_prompt
TO haku_matrix_adapter;
GRANT SELECT ON public.conversation_turn TO haku_matrix_adapter;
GRANT SELECT, UPDATE ON public.sessions TO haku_matrix_adapter;

-- 3. Identity resolution and launch authorization: reads plus the guard locks (every
-- SELECT ... FOR [NO KEY] UPDATE needs UPDATE privilege), and the anchor's updated_at stamp.
-- Deliberately no INSERT: operator/anchor rows are created by console login — a fresh
-- database that has never seen a login fails the first resolve loudly rather than letting
-- this worker fabricate identities.
GRANT SELECT, UPDATE ON
  public.operators,
  public.identity_anchors,
  public.agents,
  public.credential_bindings,
  public.static_credentials
TO haku_matrix_adapter;

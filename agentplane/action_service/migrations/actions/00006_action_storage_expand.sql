-- +goose Up
-- The current Action Service image writes both the legacy JSON and these typed fields. Keep this migration
-- additive so old replicas can continue to serve and write while the new image rolls out.
SET LOCAL lock_timeout = '1s';

ALTER TABLE action_request ADD COLUMN action_group TEXT;
ALTER TABLE action_request ADD COLUMN action_name TEXT;
ALTER TABLE action_event ADD COLUMN actor_proto BYTEA;
ALTER TABLE action_decision ADD COLUMN policy_evidence_proto BYTEA;

-- +goose Down
SET LOCAL lock_timeout = '1s';

ALTER TABLE action_decision DROP COLUMN policy_evidence_proto;
ALTER TABLE action_event DROP COLUMN actor_proto;
ALTER TABLE action_request DROP COLUMN action_name;
ALTER TABLE action_request DROP COLUMN action_group;

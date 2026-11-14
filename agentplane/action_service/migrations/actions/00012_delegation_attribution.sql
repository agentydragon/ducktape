-- +goose Up
ALTER TABLE action_request
    ADD COLUMN delegation_actor_proto BYTEA,
    ADD COLUMN provenance_session_id UUID,
    ADD COLUMN provenance_connection_id UUID,
    ADD COLUMN provenance_connection_grant_id UUID,
    ADD CONSTRAINT action_request_delegation_attribution_whole CHECK (
        (delegation_actor_proto IS NULL AND provenance_session_id IS NULL
            AND provenance_connection_id IS NULL AND provenance_connection_grant_id IS NULL)
        OR (delegation_actor_proto IS NOT NULL AND (
            (provenance_session_id IS NOT NULL AND provenance_connection_id IS NULL
                AND provenance_connection_grant_id IS NULL)
            OR (provenance_session_id IS NULL AND provenance_connection_id IS NOT NULL
                AND provenance_connection_grant_id IS NOT NULL)))
    );

COMMENT ON COLUMN action_request.delegation_actor_proto IS
    'Authenticated delegating actor at admission; NULL means the writer did not record it and is never reconstructed.';
COMMENT ON COLUMN action_request.provenance_session_id IS
    'Typed Session audit correlation; NULL means no Session provenance was recorded.';
COMMENT ON COLUMN action_request.provenance_connection_id IS
    'Typed Connection audit correlation; NULL means no Connection provenance was recorded.';
COMMENT ON COLUMN action_request.provenance_connection_grant_id IS
    'Typed Connection grant audit correlation; NULL means no Connection provenance was recorded.';

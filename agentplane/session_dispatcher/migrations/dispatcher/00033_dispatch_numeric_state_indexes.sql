-- +goose NO TRANSACTION
-- +goose Up
-- Keep the text indexes while legacy replicas may still query them. Preserve
-- valid numeric indexes across retries and rebuild interrupted concurrent builds.
-- +goose StatementBegin
DO $$
DECLARE
    invalid_index regclass;
BEGIN
    FOR invalid_index IN
        SELECT indexrelid::regclass FROM pg_index
        WHERE NOT indisvalid AND indexrelid IN (
            to_regclass('ix_dispatch_session_state_value'),
            to_regclass('ix_dispatch_sandbox_uid_state_value'),
            to_regclass('ix_dispatch_active_session_position_value'),
            to_regclass('ix_dispatch_recent_session_state_value_version'),
            to_regclass('ix_dispatch_tombstone_session_state_value_version')
        )
    LOOP
        EXECUTE format('DROP INDEX %s', invalid_index);
    END LOOP;
END
$$;
-- +goose StatementEnd

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dispatch_session_state_value
    ON dispatch (session_id, state_value);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dispatch_sandbox_uid_state_value
    ON dispatch (sandbox_uid, state_value);
-- DeliveryState values from session_dispatcher.proto: queued (1), delivering (2).
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dispatch_active_session_position_value
    ON dispatch (session_id, position) WHERE state_value IN (1, 2);
-- Admitted (3), failed (6), cancelled (7), explicitly unknown (8).
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dispatch_recent_session_state_value_version
    ON dispatch (session_id, state_value, version DESC) WHERE state_value IN (3, 6, 7, 8);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dispatch_tombstone_session_state_value_version
    ON dispatch_tombstone (session_id, state_value, version DESC);

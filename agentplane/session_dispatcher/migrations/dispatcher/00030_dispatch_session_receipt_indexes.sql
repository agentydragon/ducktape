-- +goose NO TRANSACTION
-- +goose Up
-- Concurrent builds let already-serving Dispatcher replicas keep admitting and settling commands.
-- A failed build can leave an invalid index. Clear only those remnants before retrying so
-- completed builds survive an interrupted migration and IF NOT EXISTS cannot adopt an invalid one.
-- +goose StatementBegin
DO $$
DECLARE
    invalid_index regclass;
BEGIN
    FOR invalid_index IN
        SELECT indexrelid::regclass FROM pg_index
        WHERE NOT indisvalid AND indexrelid IN (
            to_regclass('ix_dispatch_session_position'),
            to_regclass('ix_dispatch_active_session_position'),
            to_regclass('ix_dispatch_recent_session_state_version'),
            to_regclass('ix_dispatch_tombstone_session_state_version')
        )
    LOOP
        EXECUTE format('DROP INDEX %s', invalid_index);
    END LOOP;
END
$$;
-- +goose StatementEnd

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dispatch_session_position
    ON dispatch (session_id, position);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dispatch_active_session_position
    ON dispatch (session_id, position) WHERE state IN ('QUEUED_IN_DISPATCHER', 'DELIVERING_TO_RUNNER');
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dispatch_recent_session_state_version
    ON dispatch (session_id, state, version DESC)
    WHERE state IN ('FAILED', 'UNKNOWN', 'RUNNER_ADMITTED', 'CANCELLED_IN_DISPATCHER');
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dispatch_tombstone_session_state_version
    ON dispatch_tombstone (session_id, state, version DESC);

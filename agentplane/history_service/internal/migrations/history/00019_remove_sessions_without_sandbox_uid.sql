-- +goose Up
-- +goose StatementBegin
CREATE TEMP TABLE history_sessions_without_sandbox_uid ON COMMIT DROP AS
    SELECT id FROM session_history WHERE sandbox_uid IS NULL OR sandbox_uid = '';

DELETE FROM session_event_settled_range WHERE session_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM session_event_settlement WHERE session_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM session_event WHERE session_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM session_event_removed_range WHERE session_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM session_event_block WHERE session_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM session_projection_lease WHERE session_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM session_fold_pending WHERE session_id IN (SELECT id FROM history_sessions_without_sandbox_uid);

DELETE FROM thread_payload_chunk WHERE thread_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM thread_payload_manifest WHERE thread_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM thread_native_link WHERE thread_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM thread_evidence WHERE thread_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM thread_entity WHERE thread_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM thread_history_summary WHERE thread_id IN (SELECT id FROM history_sessions_without_sandbox_uid);
DELETE FROM thread_checkpoint WHERE thread_id IN (SELECT id FROM history_sessions_without_sandbox_uid);

DELETE FROM session_history WHERE id IN (SELECT id FROM history_sessions_without_sandbox_uid);
-- +goose StatementEnd

-- +goose Down
-- +goose StatementBegin
DO $$ BEGIN RAISE EXCEPTION 'History sessions without a Sandbox UID cannot be restored'; END $$;
-- +goose StatementEnd

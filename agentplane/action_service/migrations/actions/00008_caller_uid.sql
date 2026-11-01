-- +goose Up
ALTER TABLE action_request ADD COLUMN caller_uid TEXT;
ALTER TABLE action_request ADD CONSTRAINT action_request_caller_uid_nonempty
    CHECK (caller_uid IS NULL OR caller_uid <> '');
COMMENT ON COLUMN action_request.caller_uid IS
    'Verified ServiceAccount UID at admission; NULL means the admitting writer did not record it and is never backfilled.';

-- +goose Up
SET lock_timeout = '1s';
ALTER TABLE entry
    ADD COLUMN message_body TEXT,
    ADD COLUMN message_from_session TEXT,
    ADD CONSTRAINT entry_message_text_shape CHECK (
        (identity_provider = 'message' AND (message_body IS NOT NULL OR message_from_session IS NULL))
        OR
        (identity_provider IS DISTINCT FROM 'message' AND message_body IS NULL AND message_from_session IS NULL)
    ) NOT VALID,
    ADD CONSTRAINT entry_message_text_body_ceiling CHECK (
        message_body IS NULL OR octet_length(message_body) <= 262144
    ) NOT VALID,
    ADD CONSTRAINT entry_message_text_sender_length CHECK (
        message_from_session IS NULL OR char_length(message_from_session) BETWEEN 1 AND 200
    ) NOT VALID;
RESET lock_timeout;

-- +goose Down
SET lock_timeout = '1s';
ALTER TABLE entry
    DROP CONSTRAINT entry_message_text_sender_length,
    DROP CONSTRAINT entry_message_text_body_ceiling,
    DROP CONSTRAINT entry_message_text_shape;
ALTER TABLE entry
    DROP COLUMN message_body,
    DROP COLUMN message_from_session;
RESET lock_timeout;

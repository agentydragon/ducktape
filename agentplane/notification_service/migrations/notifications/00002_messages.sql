-- +goose Up
-- A message a workload sent to a Session's inbox. Its entry holds what the recipient reads: the body, and the
-- sender in the event identity. This row is what the sender's retries and receipts find the message by.
CREATE TABLE message (
    id UUID PRIMARY KEY,
    inbox_id UUID NOT NULL,
    cursor BIGINT NOT NULL,
    sender_namespace VARCHAR NOT NULL,
    sender_name VARCHAR NOT NULL,
    idempotency_key VARCHAR NOT NULL,
    -- SHA-256 of the payload as sent, which a retry under the same key must match after the payload expires.
    digest BYTEA NOT NULL,
    UNIQUE (inbox_id, cursor),
    UNIQUE (inbox_id, sender_namespace, sender_name, idempotency_key),
    FOREIGN KEY (inbox_id, cursor) REFERENCES entry (inbox_id, cursor) ON DELETE CASCADE
);

-- The absolute ceiling of a message body, in UTF-8 bytes; the service's configured limit is at most this.
ALTER TABLE entry ADD CONSTRAINT entry_message_body_ceiling CHECK (
    event ->> 'provider' IS DISTINCT FROM 'message' OR payload IS NULL OR octet_length(payload ->> 'body') <= 262144);

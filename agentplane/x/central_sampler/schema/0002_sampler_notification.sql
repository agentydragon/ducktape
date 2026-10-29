-- +goose Up
-- notify() outputs acknowledged to the code-mode host and awaiting the turn's next drain point.
CREATE TABLE sampler_notification (
    conversation_id UUID NOT NULL REFERENCES sampler_conversation(id),
    sequence BIGINT GENERATED ALWAYS AS IDENTITY,
    call_id VARCHAR NOT NULL,
    cell_id VARCHAR NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (conversation_id, sequence)
);

-- +goose Down
-- Durable sampler state has no safe downgrade. Restore a database backup to revert a migration.

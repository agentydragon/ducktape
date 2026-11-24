-- +goose Up
CREATE TABLE session_event_catalog (
    session_id UUID NOT NULL REFERENCES session_history(id) ON DELETE CASCADE,
    digest BYTEA NOT NULL CHECK (octet_length(digest) = 32),
    payload BYTEA NOT NULL CHECK (octet_length(payload) >= 1024),
    PRIMARY KEY (session_id, digest)
);

-- Older rows have null reference columns. Enforce these constraints on writes without scanning
-- the existing event table under the startup migration lock.
ALTER TABLE session_event
    ADD COLUMN catalog_id BYTEA,
    ADD COLUMN catalog_offset INTEGER,
    ADD COLUMN catalog_size INTEGER,
    ADD CONSTRAINT session_event_catalog_fk FOREIGN KEY (session_id, catalog_id)
        REFERENCES session_event_catalog (session_id, digest) NOT VALID,
    ADD CONSTRAINT session_event_catalog_reference CHECK (
        (catalog_id IS NULL AND catalog_offset IS NULL AND catalog_size IS NULL)
        OR (catalog_id IS NOT NULL AND catalog_offset IS NOT NULL AND catalog_size IS NOT NULL
            AND catalog_offset BETWEEN 0 AND octet_length(payload)
            AND catalog_size >= 1024 AND NOT compactable)
    ) NOT VALID;

-- +goose Up
CREATE TABLE sampler_event_block (
    session_id VARCHAR NOT NULL REFERENCES sampler_session(id),
    first_sequence INTEGER NOT NULL CHECK (first_sequence > 0),
    last_sequence INTEGER NOT NULL CHECK (last_sequence > first_sequence AND last_sequence - first_sequence < 32),
    -- Gzip over consecutive frames, each prefixed with its uint32 big-endian byte length.
    payload BYTEA NOT NULL,
    PRIMARY KEY (session_id, first_sequence)
);

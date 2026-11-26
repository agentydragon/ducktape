-- +goose Up
CREATE TABLE history_event_repack_run (
    profile_hash BYTEA PRIMARY KEY CHECK (octet_length(profile_hash) = 32),
    profile JSONB NOT NULL,
    opening_position BIGINT NOT NULL CHECK (opening_position >= 0),
    after_position BIGINT NOT NULL DEFAULT 0 CHECK (after_position >= 0),
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE history_event_repack_checkpoint (
    profile_hash BYTEA NOT NULL CHECK (octet_length(profile_hash) = 32),
    session_id UUID NOT NULL REFERENCES session_history(id) ON DELETE CASCADE,
    recorded_position BIGINT NOT NULL CHECK (recorded_position > 0),
    through_cursor BIGINT NOT NULL CHECK (through_cursor >= 0),
    start_cursor BIGINT NOT NULL DEFAULT 0 CHECK (start_cursor >= 0 AND start_cursor <= through_cursor),
    event_kind INTEGER NOT NULL CHECK (event_kind >= 1),
    phase SMALLINT NOT NULL CHECK (phase IN (0, 1)),
    unpack_after BIGINT NOT NULL DEFAULT 0 CHECK (unpack_after >= 0),
    pack_after BIGINT NOT NULL DEFAULT 0 CHECK (pack_after >= 0),
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_hash, session_id)
);

CREATE INDEX ix_history_event_repack_incomplete
    ON history_event_repack_checkpoint(profile_hash, recorded_position)
    WHERE NOT complete;

-- +goose Down
DROP TABLE history_event_repack_checkpoint;
DROP TABLE history_event_repack_run;

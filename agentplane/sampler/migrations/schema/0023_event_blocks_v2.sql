-- +goose Up
ALTER TABLE sampler_event_block
    DROP CONSTRAINT sampler_event_block_check,
    ADD CONSTRAINT sampler_event_block_range_check
        CHECK (last_sequence >= first_sequence AND last_sequence - first_sequence < 256);

CREATE TABLE sampler_event_block_checkpoint (
    session_id VARCHAR NOT NULL REFERENCES sampler_session(id) ON DELETE CASCADE,
    profile_fingerprint VARCHAR(64) NOT NULL,
    opening_sequence INTEGER NOT NULL CHECK (opening_sequence >= 0),
    through_sequence INTEGER NOT NULL CHECK (through_sequence >= 0 AND through_sequence <= opening_sequence),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, profile_fingerprint)
);

-- +goose Down
DROP TABLE sampler_event_block_checkpoint;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM sampler_event_block
        WHERE last_sequence <= first_sequence OR last_sequence - first_sequence >= 32
    ) THEN
        RAISE EXCEPTION 'cannot restore the legacy event-block constraint while singleton or 32+ entry blocks remain';
    END IF;
END $$;

ALTER TABLE sampler_event_block
    DROP CONSTRAINT sampler_event_block_range_check,
    ADD CONSTRAINT sampler_event_block_check
        CHECK (last_sequence > first_sequence AND last_sequence - first_sequence < 32);

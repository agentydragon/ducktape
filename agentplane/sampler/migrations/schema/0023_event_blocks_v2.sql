-- +goose Up
ALTER TABLE sampler_event_block
    DROP CONSTRAINT sampler_event_block_check,
    ADD CONSTRAINT sampler_event_block_range_check
        CHECK (last_sequence >= first_sequence AND last_sequence - first_sequence < 256);

-- +goose Down
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

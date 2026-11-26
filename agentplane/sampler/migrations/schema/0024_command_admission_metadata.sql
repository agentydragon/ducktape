-- +goose Up
-- Keep the existing command payload as the single copy of the admitted Command. The existing
-- admission cursor also serves as the local EventOrigin sequence; only the other response metadata
-- is stored here. Nullable columns let older binaries continue writing during a rolling upgrade.
ALTER TABLE sampler_command
    ADD COLUMN admission_origin_source_id TEXT,
    ADD COLUMN admission_event_at_seconds BIGINT,
    ADD COLUMN admission_event_at_nanos INTEGER,
    ADD CONSTRAINT sampler_command_admission_metadata_check CHECK (
        (admission_origin_source_id IS NULL AND admission_event_at_seconds IS NULL AND admission_event_at_nanos IS NULL)
        OR
        (admission_origin_source_id IS NOT NULL AND admission_event_at_seconds IS NOT NULL AND admission_event_at_nanos IS NOT NULL
            AND admission_event_at_nanos >= 0 AND admission_event_at_nanos < 1000000000)
    );

-- +goose Down
ALTER TABLE sampler_command
    DROP CONSTRAINT sampler_command_admission_metadata_check,
    DROP COLUMN admission_origin_source_id,
    DROP COLUMN admission_event_at_seconds,
    DROP COLUMN admission_event_at_nanos;

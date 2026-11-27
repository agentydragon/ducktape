-- +goose Up
ALTER TABLE sampler_intent
    ADD COLUMN dispatch_turn_id VARCHAR,
    ADD COLUMN dispatch_sequence INTEGER,
    ADD CONSTRAINT sampler_intent_dispatch_projection_check CHECK (
        (dispatch_turn_id IS NULL AND dispatch_sequence IS NULL)
        OR (dispatch_turn_id IS NOT NULL AND dispatch_sequence IS NOT NULL AND dispatch_sequence > 0)
    );

CREATE TABLE sampler_intent_source (
    intent_id UUID NOT NULL REFERENCES sampler_intent(id) ON DELETE CASCADE,
    source_sequence INTEGER NOT NULL CHECK (source_sequence > 0),
    PRIMARY KEY (intent_id, source_sequence)
);

CREATE TABLE sampler_intent_started_item (
    intent_id UUID NOT NULL REFERENCES sampler_intent(id) ON DELETE CASCADE,
    item_id VARCHAR NOT NULL,
    PRIMARY KEY (intent_id, item_id)
);

-- Older sampler binaries append journal rows without maintaining the recovery projection. Mark
-- any projected pending sampling attempt incomplete before such evidence can be recovered. New
-- binaries set a transaction-local flag after updating the projection atomically with the append.
-- +goose StatementBegin
CREATE FUNCTION sampler_invalidate_sampling_recovery_projection() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('agentplane.sampler_indexed_recovery_writer', TRUE) = 'on' THEN
        RETURN NULL;
    END IF;

    UPDATE sampler_intent AS intent
    SET dispatch_turn_id = NULL,
        dispatch_sequence = NULL
    FROM sampler_session AS session
    JOIN sampler_conversation AS conversation ON conversation.id = session.conversation_id
    WHERE session.id = NEW.session_id
      AND intent.id::text = conversation.pending->>'id'
      AND intent.request->>'kind' = 'sampling'
      AND intent.dispatch_turn_id IS NOT NULL;

    RETURN NULL;
END;
$$;
-- +goose StatementEnd

CREATE TRIGGER sampler_event_invalidates_sampling_recovery_projection
    AFTER INSERT ON sampler_event
    FOR EACH ROW EXECUTE FUNCTION sampler_invalidate_sampling_recovery_projection();

-- +goose Down
DROP TRIGGER sampler_event_invalidates_sampling_recovery_projection ON sampler_event;
DROP FUNCTION sampler_invalidate_sampling_recovery_projection();
DROP TABLE sampler_intent_started_item;
DROP TABLE sampler_intent_source;
ALTER TABLE sampler_intent
    DROP CONSTRAINT sampler_intent_dispatch_projection_check,
    DROP COLUMN dispatch_turn_id,
    DROP COLUMN dispatch_sequence;

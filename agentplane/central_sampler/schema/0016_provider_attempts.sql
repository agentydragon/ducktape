-- +goose Up
ALTER TABLE sampler_intent
    ADD COLUMN provider_step_id UUID,
    ADD COLUMN provider_attempt_no SMALLINT,
    ADD COLUMN provider_attempt_budget SMALLINT,
    ADD COLUMN provider_attempt_lease_until TIMESTAMPTZ,
    ADD COLUMN provider_retry_not_before TIMESTAMPTZ,
    ADD COLUMN previous_provider_intent_id UUID REFERENCES sampler_intent(id),
    ADD COLUMN provider_command_id VARCHAR,
    ADD CONSTRAINT sampler_intent_provider_step_fk
        FOREIGN KEY (provider_step_id) REFERENCES sampler_intent(id)
        DEFERRABLE INITIALLY DEFERRED;

-- Existing attempts have no recorded retry policy. Treat each as a one-attempt step, and do not
-- invent retry work. An unresolved attempt expires no later than its current Session owner lease.
UPDATE sampler_intent i
SET provider_step_id = i.id,
    provider_attempt_no = 1,
    provider_attempt_budget = 1,
    provider_attempt_lease_until = CASE
        WHEN i.result IS NULL THEN COALESCE(c.lease_until, clock_timestamp())
        ELSE clock_timestamp()
    END,
    provider_command_id = CASE
        WHEN i.result IS NULL AND c.pending->>'id' = i.id::text THEN s.active_command
        ELSE NULL
    END
FROM sampler_conversation c
LEFT JOIN sampler_session s ON s.conversation_id = c.id
WHERE c.id = i.conversation_id
  AND i.request->>'kind' IN ('sampling', 'compaction');

ALTER TABLE sampler_intent
    ADD CONSTRAINT sampler_intent_provider_attempt_fields_check CHECK (
        (request->>'kind' IN ('sampling', 'compaction')
            AND provider_step_id IS NOT NULL
            AND provider_attempt_no IS NOT NULL AND provider_attempt_no > 0
            AND provider_attempt_budget IS NOT NULL AND provider_attempt_budget >= provider_attempt_no
            AND provider_attempt_lease_until IS NOT NULL
            AND ((provider_attempt_no = 1 AND provider_step_id = id AND previous_provider_intent_id IS NULL)
                OR (provider_attempt_no > 1 AND previous_provider_intent_id IS NOT NULL)))
        OR
        (request->>'kind' NOT IN ('sampling', 'compaction')
            AND provider_step_id IS NULL
            AND provider_attempt_no IS NULL
            AND provider_attempt_budget IS NULL
            AND provider_attempt_lease_until IS NULL
            AND provider_retry_not_before IS NULL
            AND previous_provider_intent_id IS NULL
            AND provider_command_id IS NULL)
    );

CREATE UNIQUE INDEX sampler_intent_provider_attempt_step_idx
    ON sampler_intent(provider_step_id, provider_attempt_no)
    WHERE provider_step_id IS NOT NULL;
CREATE UNIQUE INDEX sampler_intent_provider_predecessor_idx
    ON sampler_intent(previous_provider_intent_id)
    WHERE previous_provider_intent_id IS NOT NULL;
CREATE INDEX sampler_intent_provider_retry_due_idx
    ON sampler_intent(provider_retry_not_before, conversation_id, provider_step_id)
    WHERE provider_retry_not_before IS NOT NULL;

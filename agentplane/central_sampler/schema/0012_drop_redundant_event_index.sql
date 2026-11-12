-- +goose Up
-- sampler_event's primary key already indexes (session_id, sequence) in the same order.
DROP INDEX sampler_event_sequence_idx;

-- +goose Down
CREATE INDEX sampler_event_sequence_idx ON sampler_event(session_id, sequence);

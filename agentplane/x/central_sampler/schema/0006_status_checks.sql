-- +goose Up
ALTER TABLE sampler_process ADD CONSTRAINT sampler_process_status_check
    CHECK (status IN ('starting', 'running', 'exited', 'exited_unread', 'abandoned', 'unknown'));
ALTER TABLE sampler_cell ADD CONSTRAINT sampler_cell_status_check
    CHECK (status IN ('live', 'completed', 'terminated', 'abandoned'));

-- +goose Down
ALTER TABLE sampler_process DROP CONSTRAINT sampler_process_status_check;
ALTER TABLE sampler_cell DROP CONSTRAINT sampler_cell_status_check;

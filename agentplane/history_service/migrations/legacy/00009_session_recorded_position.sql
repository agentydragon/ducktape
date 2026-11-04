-- +goose Up
-- Rename the durable recording cursor without changing any assigned position or its
-- identity sequence. The sequence remains owned by the renamed column.
ALTER TABLE session_history RENAME COLUMN opened_position TO recorded_position;
ALTER SEQUENCE session_history_opened_position_seq RENAME TO session_history_recorded_position_seq;
ALTER INDEX ux_history_opened_position RENAME TO ux_history_recorded_position;
ALTER INDEX ix_history_execution_principal_opened RENAME TO ix_history_execution_principal_recorded;

-- +goose Down
ALTER INDEX ix_history_execution_principal_recorded RENAME TO ix_history_execution_principal_opened;
ALTER INDEX ux_history_recorded_position RENAME TO ux_history_opened_position;
ALTER SEQUENCE session_history_recorded_position_seq RENAME TO session_history_opened_position_seq;
ALTER TABLE session_history RENAME COLUMN recorded_position TO opened_position;

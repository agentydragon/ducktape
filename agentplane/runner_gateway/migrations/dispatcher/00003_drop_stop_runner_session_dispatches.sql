-- +goose Up
-- No RPC accepts a stop command and no runner executes one, so its stored receipts are dropped
-- whatever their delivery state.
DELETE FROM dispatch WHERE operation = 'stop_runner_session';

-- +goose Up
ALTER TABLE action_execution DROP COLUMN heartbeat_at;

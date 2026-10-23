-- +goose Up
ALTER TABLE external_connection_grant DROP COLUMN activation_deadline;

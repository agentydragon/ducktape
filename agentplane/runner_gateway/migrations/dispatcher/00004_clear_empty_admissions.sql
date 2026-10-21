-- +goose Up
-- Settling without an admission stored an empty one; only a runner's admission is recorded.
UPDATE dispatch SET admission = NULL WHERE admission = ''::bytea;

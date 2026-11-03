-- +goose Up
-- Retained independently of queue/ownership cleanup: an old inventory read must
-- never reopen admission for this exact UID after deletion commits.
CREATE TABLE deleted_sandbox (
    sandbox_uid UUID PRIMARY KEY
);

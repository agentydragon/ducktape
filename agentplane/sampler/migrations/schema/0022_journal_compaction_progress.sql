-- +goose Up
ALTER TABLE sampler_session ADD COLUMN journal_compacted_through INTEGER NOT NULL DEFAULT 0;

-- +goose Up
ALTER TABLE github_pull_request
    ADD COLUMN merge_commit_sha VARCHAR,
    ADD COLUMN mergeability_recheck_attempt INTEGER NOT NULL DEFAULT 0
        CHECK (mergeability_recheck_attempt >= 0);

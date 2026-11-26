-- +goose Up
-- Keep a compact event-specific fact beside each receipt. Old writers leave it NULL; new readers
-- backfill those rows from the still-retained signed body before matching or expiring the body.
ALTER TABLE github_delivery ADD COLUMN fact jsonb;

CREATE INDEX ix_github_delivery_unprojected
    ON github_delivery ("position") WHERE fact IS NULL;

-- +goose Down
DROP INDEX ix_github_delivery_unprojected;
ALTER TABLE github_delivery DROP COLUMN fact;

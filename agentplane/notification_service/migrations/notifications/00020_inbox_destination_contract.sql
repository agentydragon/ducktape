-- +goose Up
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- A validated proof is a catalog-only gate: it already covers retained rows and all later writes. The common
-- populated path therefore avoids scanning inbox or consulting the backfill checkpoint. A fresh empty install
-- may validate inline; the NOT VALID check already rejects invalid concurrent writes during that scan.
-- +goose StatementBegin
DO $$
DECLARE
    inbox_has_rows BOOLEAN;
    destination_shape_validated BOOLEAN;
BEGIN
    SELECT convalidated INTO destination_shape_validated
    FROM pg_constraint
    WHERE conrelid = 'inbox'::regclass AND conname = 'inbox_destination_fields_valid';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'inbox destination shape constraint is missing; apply 00016_inbox_destination_typed_only.sql first';
    END IF;

    IF NOT destination_shape_validated THEN
        SELECT EXISTS (SELECT 1 FROM inbox LIMIT 1) INTO inbox_has_rows;
        IF inbox_has_rows THEN
            RAISE EXCEPTION 'inbox destination shape proof is unvalidated on a populated database; init validation must finish before this contract';
        END IF;

        -- On a fresh empty database the table scan is bounded. The NOT VALID check still applies to concurrent
        -- inserts, so any row created during this validation must satisfy the typed destination shape.
        ALTER TABLE inbox VALIDATE CONSTRAINT inbox_destination_fields_valid;
    END IF;
END $$;
-- +goose StatementEnd

-- PostgreSQL can use the validated shape proof to avoid a table scan while setting NOT NULL. Keep the CHECK after
-- the cutover because it also enforces the destination string-length limits for future writes.
ALTER TABLE inbox
    ALTER COLUMN destination_namespace SET NOT NULL,
    ALTER COLUMN destination_name SET NOT NULL,
    ALTER COLUMN destination_uid SET NOT NULL;

ALTER TABLE inbox DROP COLUMN destination_ref;

-- The progress table also tracks subscription creation, so only retire the destination checkpoint columns here.
ALTER TABLE notification_storage_backfill_progress
    DROP COLUMN inbox_destination_complete,
    DROP COLUMN inbox_destination_last_id;

-- +goose Down
SET LOCAL lock_timeout = '5s';

-- Reconstructing dropped JSON for all inboxes would be an unbounded table rewrite; retain this contract.
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'inbox destination JSON was retired by the contract migration; retain this migration';
END $$;
-- +goose StatementEnd

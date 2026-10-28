-- +goose Up
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- A populated table must carry durable proof from the offline typed-only writer validation. Fresh databases reach
-- this migration with the same four constraints still NOT VALID, so validate them here only while the table is
-- empty. The source-provider and source-state checks remain permanent relational invariants; the storage-pair and
-- typed-required checks are removed after they prove the new columns are safe to make NOT NULL. Inspect catalog
-- proofs first to avoid even an empty-table probe after offline validation has completed.
-- +goose StatementBegin
DO $$
DECLARE
    proof_count integer;
    validated_count integer;
BEGIN
    SELECT count(*), count(*) FILTER (WHERE convalidated)
    INTO proof_count, validated_count
    FROM pg_constraint
    WHERE conrelid = 'subscription'::regclass
      AND conname = ANY (ARRAY[
          'subscription_creation_storage_pair',
          'subscription_source_provider',
          'subscription_typed_source_state',
          'subscription_creation_typed_required'
      ]);

    IF proof_count <> 4 THEN
        IF EXISTS (SELECT 1 FROM subscription LIMIT 1) THEN
            RAISE EXCEPTION 'subscription creation contract requires all four validated typed-storage constraints on a populated database; missing %', 4 - proof_count;
        END IF;
        RAISE EXCEPTION 'subscription creation contract is missing % required typed-storage constraints', 4 - proof_count;
    END IF;

    IF validated_count = proof_count THEN
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM subscription LIMIT 1) THEN
        RAISE EXCEPTION 'subscription creation contract requires all four typed-storage constraints to be validated before dropping creation JSON; run notification-storage-validate --proof=subscription-creation from the typed-only release after every writer is typed-only';
    END IF;

    ALTER TABLE subscription VALIDATE CONSTRAINT subscription_creation_storage_pair;
    ALTER TABLE subscription VALIDATE CONSTRAINT subscription_source_provider;
    ALTER TABLE subscription VALIDATE CONSTRAINT subscription_typed_source_state;
    ALTER TABLE subscription VALIDATE CONSTRAINT subscription_creation_typed_required;
END $$;
-- +goose StatementEnd

-- The validated NOT NULL check lets PostgreSQL set these attributes without a second table scan under the
-- ACCESS EXCLUSIVE lock. The retained JSON was only a rollback copy and is no longer read by serving code.
ALTER TABLE subscription
    ALTER COLUMN creation_proto SET NOT NULL,
    ALTER COLUMN source_provider SET NOT NULL;

ALTER TABLE subscription
    DROP CONSTRAINT subscription_creation_typed_required,
    DROP CONSTRAINT subscription_creation_storage_pair,
    DROP COLUMN creation;

DROP TABLE notification_storage_backfill_progress;

-- +goose Down
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '30s';

-- Creation JSON was dropped; empty columns would falsely imply rollback preserved subscription data.
-- +goose StatementBegin
DO $$
BEGIN
    RAISE EXCEPTION 'subscription creation contract is irreversible; restore a pre-contract database or deploy code compatible with typed-only storage';
END $$;
-- +goose StatementEnd

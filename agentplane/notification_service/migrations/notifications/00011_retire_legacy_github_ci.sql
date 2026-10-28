-- +goose Up
-- The operator accepted losing fourteen historical CI facts written by old workers after migration 7's
-- copy. Their source sequence numbers collided with native observations; replay would risk duplicate
-- notifications. Inbox copies and all native observation identities remain unchanged.
LOCK TABLE github_ci_subscription_snapshot IN ACCESS EXCLUSIVE MODE;

-- +goose StatementBegin
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM github_ci_subscription_snapshot) THEN
        RAISE EXCEPTION 'Legacy GitHub CI snapshots remain pending; reconcile them before retiring the legacy tables';
    END IF;
END $$;
-- +goose StatementEnd

DROP TABLE github_ci_subscription_snapshot, github_ci_refresh, github_ci_current, github_ci_event;
ALTER TABLE subscription DROP COLUMN github_ci_after_sequence;
ALTER TABLE github_delivery DROP COLUMN ci_normalized;

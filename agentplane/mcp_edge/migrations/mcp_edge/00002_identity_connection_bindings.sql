-- +goose Up
-- Preserve each pre-cutover binding as historical attribution, then pause it for explicit operator review. Nothing
-- in that old field remains an active authentication path.
ALTER TABLE external_connection ADD COLUMN legacy_bound_caller JSONB;
ALTER TABLE external_connection ADD COLUMN bound_identity JSONB;
ALTER TABLE external_connection ADD COLUMN identity_binding_required BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE external_connection
SET legacy_bound_caller = bound_caller,
    identity_binding_required = (bound_caller IS NOT NULL);
-- Keep the old column inert so old replicas can read the schema during rollout; every migrated row is unbound there.
UPDATE external_connection SET bound_caller = NULL;
-- Old binaries may still read the column, but must not restore SA authority during a rolling deployment.
ALTER TABLE external_connection ADD CONSTRAINT external_connection_bound_caller_retired CHECK (bound_caller IS NULL);

-- New grants and enrollment decisions pin an Identity name and UID. Keep the prior account facts byte-for-byte in
-- their existing columns for audit, and permit new rows to leave those historical fields empty.
ALTER TABLE external_connection_grant ADD COLUMN original_identity JSONB;
ALTER TABLE external_connection_grant ALTER COLUMN original_caller DROP NOT NULL;
ALTER TABLE connection_enrollment ADD COLUMN caller_identity JSONB;

-- +goose Up
-- Legacy Notification inboxes and their dependent rows were deleted in staging before cutover.
-- Reject an incomplete owner instead of retaining a legacy inbox representation.
ALTER TABLE inbox
    DROP COLUMN owner_uid,
    ALTER COLUMN owner_identity_name SET NOT NULL,
    ALTER COLUMN owner_identity_uid SET NOT NULL;

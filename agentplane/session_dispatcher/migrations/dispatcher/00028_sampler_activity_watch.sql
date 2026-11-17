-- +goose Up
-- Keep the hosted key and table usable by rollback images. NULL sandbox_uid is
-- reserved for the single sampler row; existing hosted rows are unchanged.
ALTER TABLE sandbox_activity_watch DROP CONSTRAINT sandbox_activity_watch_pkey;
ALTER TABLE sandbox_activity_watch ALTER COLUMN sandbox_uid DROP NOT NULL;
ALTER TABLE sandbox_activity_watch ADD UNIQUE (sandbox_uid);
ALTER TABLE sandbox_activity_watch ADD COLUMN central_sampler boolean UNIQUE;
ALTER TABLE sandbox_activity_watch ADD CONSTRAINT activity_host_key CHECK (
    (sandbox_uid IS NOT NULL AND central_sampler IS NULL)
    OR (sandbox_uid IS NULL AND central_sampler IS TRUE)
);

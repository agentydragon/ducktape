-- +goose Up
-- The Identity incarnation the Session authority recorded the inbox's Session as executing as.
-- NULL is unestablished; only the Session authority establishes it, and an existing Identity of
-- the same name is not ownership evidence. Additive: a replica predating it reads inboxes as before.
ALTER TABLE inbox
    ADD COLUMN owner_identity_name text,
    ADD COLUMN owner_identity_uid text,
    ADD CONSTRAINT inbox_owner_identity_complete
        CHECK ((owner_identity_name IS NULL) = (owner_identity_uid IS NULL)
            AND (owner_identity_uid IS NULL OR (owner_identity_name <> '' AND owner_identity_uid <> '')));

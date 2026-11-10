-- +goose Up
-- NULL is an unestablished identity. Only the Session authority can establish it;
-- a currently existing account with the same name is not ownership evidence.
ALTER TABLE inbox ADD COLUMN owner_uid text CHECK (owner_uid IS NULL OR owner_uid <> '');
ALTER TABLE message ADD COLUMN sender_uid text CHECK (sender_uid IS NULL OR sender_uid <> '');

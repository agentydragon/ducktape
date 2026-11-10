-- +goose Up
-- The Identity incarnation a workload SVID naming an Identity sent the message as. Such a message has
-- no sender_uid, and its sender_namespace and sender_name name the Sandbox it was sent from, the
-- ServiceAccount a Sandbox ran as before it ran as an Identity. Additive: a replica predating it
-- reads such a message as one with an unestablished sender UID, which no sender can read back.
ALTER TABLE message
    ADD COLUMN sender_identity_name text,
    ADD COLUMN sender_identity_uid text,
    ADD CONSTRAINT message_sender_identity_complete
        CHECK ((sender_identity_name IS NULL) = (sender_identity_uid IS NULL)
            AND (sender_identity_uid IS NULL OR (sender_identity_name <> '' AND sender_identity_uid <> '' AND sender_uid IS NULL)));

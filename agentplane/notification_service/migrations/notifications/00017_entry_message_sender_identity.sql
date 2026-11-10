-- +goose Up
-- The Identity incarnation an Identity sender sent a message as, shown to the recipient beside
-- message_sender_{namespace,name}, which name the Sandbox it sent from; NULL for a ServiceAccount
-- sender. Additive: a replica predating it shows such a message by its Sandbox alone.
ALTER TABLE entry
    ADD COLUMN message_sender_identity_name text,
    ADD COLUMN message_sender_identity_uid text,
    ADD CONSTRAINT entry_message_sender_identity_complete
        CHECK ((message_sender_identity_name IS NULL) = (message_sender_identity_uid IS NULL)
            AND (message_sender_identity_uid IS NULL
                OR (message_sender_identity_name <> '' AND message_sender_identity_uid <> '' AND identity_provider = 'message')));

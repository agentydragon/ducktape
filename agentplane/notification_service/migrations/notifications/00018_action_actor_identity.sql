-- +goose Up
-- Identity cancellation actors retain their incarnation independently of ServiceAccount provenance.
-- Existing writers omit these nullable columns and keep their unchanged caller/operator forms.
ALTER TABLE entry
    ADD COLUMN action_actor_identity_name text,
    ADD COLUMN action_actor_identity_uid text,
    ADD CONSTRAINT entry_action_actor_identity_length CHECK (
        (action_actor_identity_name IS NULL OR char_length(action_actor_identity_name) BETWEEN 1 AND 253) AND
        (action_actor_identity_uid IS NULL OR char_length(action_actor_identity_uid) > 0)
    );

ALTER TABLE entry DROP CONSTRAINT entry_action_content_shape;
ALTER TABLE entry ADD CONSTRAINT entry_action_content_shape CHECK (
    (action_at IS NULL AND action_state IS NULL AND action_actor_kind IS NULL AND
     action_actor_namespace IS NULL AND action_actor_name IS NULL AND
     action_actor_issuer IS NULL AND action_actor_subject IS NULL AND
     action_actor_identity_name IS NULL AND action_actor_identity_uid IS NULL)
    OR (action_at IS NOT NULL AND action_state IS NOT NULL AND (
        (action_actor_kind IS NULL AND action_actor_namespace IS NULL AND action_actor_name IS NULL AND
         action_actor_issuer IS NULL AND action_actor_subject IS NULL AND
         action_actor_identity_name IS NULL AND action_actor_identity_uid IS NULL)
        OR (action_actor_kind = 'caller' AND action_actor_kind IS NOT NULL AND
            action_actor_namespace IS NOT NULL AND action_actor_name IS NOT NULL AND
            action_actor_issuer IS NULL AND action_actor_subject IS NULL AND
            action_actor_identity_name IS NULL AND action_actor_identity_uid IS NULL)
        OR (action_actor_kind = 'operator' AND action_actor_kind IS NOT NULL AND
            action_actor_namespace IS NULL AND action_actor_name IS NULL AND
            action_actor_issuer IS NOT NULL AND action_actor_subject IS NOT NULL AND
            action_actor_identity_name IS NULL AND action_actor_identity_uid IS NULL)
        OR (action_actor_kind = 'identity' AND action_actor_kind IS NOT NULL AND
            action_actor_namespace IS NULL AND action_actor_name IS NULL AND
            action_actor_issuer IS NULL AND action_actor_subject IS NULL AND
            action_actor_identity_name IS NOT NULL AND action_actor_identity_uid IS NOT NULL)
    ))
);

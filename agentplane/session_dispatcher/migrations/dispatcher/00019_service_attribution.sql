-- +goose Up
-- Certificate actors never populate account or Pod proof. Additive columns keep overlapping
-- old writers valid; old readers see service-attributed rows as unattributed.
ALTER TABLE dispatch ADD COLUMN service_attribution JSONB,
    ADD CONSTRAINT ck_dispatch_service_attribution CHECK (service_attribution IS NULL OR
        (jsonb_typeof(service_attribution) = 'object' AND
        num_nonnulls(actor_account_namespace, actor_identity_name, delegated_identity_name) = 0));
ALTER TABLE session ADD COLUMN service_attribution JSONB,
    ADD CONSTRAINT ck_session_service_attribution CHECK (service_attribution IS NULL OR
        (jsonb_typeof(service_attribution) = 'object' AND
        num_nonnulls(creator_account_namespace, creator_identity_name) = 0));

CREATE TABLE service_wake_attempt (
    service_spiffe_id VARCHAR NOT NULL,
    sandbox_uid UUID NOT NULL,
    wake_generation BIGINT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (service_spiffe_id, sandbox_uid, wake_generation)
);
CREATE INDEX ix_service_wake_attempt_time ON service_wake_attempt (attempted_at);

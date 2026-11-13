-- +goose Up
-- Nullable, so replicas still writing the earlier columns keep inserting during a roll.
ALTER TABLE egress_decision
    ADD COLUMN IF NOT EXISTS impersonated_namespace TEXT,
    ADD COLUMN IF NOT EXISTS impersonated_name TEXT,
    ADD CONSTRAINT egress_decision_impersonated_whole
        CHECK ((impersonated_namespace IS NULL) = (impersonated_name IS NULL));

-- +goose Up
ALTER TABLE sampler_session ADD COLUMN service_spiffe_id TEXT;
ALTER TABLE sampler_session ADD CONSTRAINT sampler_session_owner_kind CHECK (
    (service_spiffe_id IS NULL AND account_uid <> '') OR
    (service_spiffe_id IS NOT NULL AND service_spiffe_id LIKE 'spiffe://agentplane/%' AND account_uid = '')
);

-- +goose Down
-- Refuse a rollback that would orphan sessions already created by service peers.
-- +goose StatementBegin
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM sampler_session WHERE service_spiffe_id IS NOT NULL) THEN
        RAISE EXCEPTION 'service-owned sampler sessions require ownership cutover before rollback';
    END IF;
END $$;
-- +goose StatementEnd
ALTER TABLE sampler_session DROP CONSTRAINT sampler_session_owner_kind;
ALTER TABLE sampler_session DROP COLUMN service_spiffe_id;

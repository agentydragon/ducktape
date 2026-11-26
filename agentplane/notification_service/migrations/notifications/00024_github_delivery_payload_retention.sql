-- +goose Up
-- Receipt facts and matching metadata outlive this optional raw webhook body.
ALTER TABLE github_delivery ALTER COLUMN payload DROP NOT NULL;
ALTER TABLE github_delivery ADD CONSTRAINT github_delivery_payload_or_fact_required
    CHECK (payload IS NOT NULL OR fact IS NOT NULL);

-- +goose Down
-- A PR1-capable reader can continue from facts if expired bodies cannot be restored.
ALTER TABLE github_delivery DROP CONSTRAINT github_delivery_payload_or_fact_required;
UPDATE github_delivery SET payload = fact WHERE payload IS NULL AND fact IS NOT NULL;
ALTER TABLE github_delivery ALTER COLUMN payload SET NOT NULL;

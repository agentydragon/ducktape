-- +goose Up
-- Every Connection is bound to an Identity or unbound: staging held no ServiceAccount-bound Connection when the
-- serving code stopped reading these columns. Grants keep their historical original_caller.
ALTER TABLE external_connection DROP COLUMN legacy_bound_caller;
ALTER TABLE external_connection DROP COLUMN identity_binding_required;
ALTER TABLE external_connection DROP COLUMN bound_caller;

-- +goose Up
-- Old checkpoints acknowledged entries before the announced metadata finished committing.
-- Reconcile those snapshots once under the new completion contract. Only delivery markers are
-- discarded; the sampler journal and History entries remain intact.
DELETE FROM sampler_export_checkpoint;

-- +goose Down
-- A missing delivery marker is safe for either exporter: reconnect reconciles History's cursor.

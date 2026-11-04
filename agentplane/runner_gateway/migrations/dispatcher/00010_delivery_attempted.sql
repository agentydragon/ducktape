-- +goose Up
ALTER TABLE dispatch ADD COLUMN delivery_attempted boolean NOT NULL DEFAULT false;

-- +goose Down
ALTER TABLE dispatch DROP COLUMN delivery_attempted;

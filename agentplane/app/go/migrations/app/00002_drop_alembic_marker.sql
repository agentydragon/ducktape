-- +goose Up
-- The Go runtime no longer uses the Alembic rollback marker. Version 1 may have
-- created it on an empty database or retained it when adopting the Python schema.
DROP TABLE IF EXISTS alembic_version_app;

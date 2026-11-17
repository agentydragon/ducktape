-- +goose Up
ALTER TABLE session ADD COLUMN activity bytea;
ALTER TABLE session ADD COLUMN activity_epoch uuid;

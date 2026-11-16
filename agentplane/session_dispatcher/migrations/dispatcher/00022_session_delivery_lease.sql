-- +goose Up
CREATE TABLE session_delivery_lease (
    session_id VARCHAR PRIMARY KEY,
    holder UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

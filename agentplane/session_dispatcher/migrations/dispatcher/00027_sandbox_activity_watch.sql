-- +goose Up
CREATE TABLE sandbox_activity_watch (
    sandbox_uid uuid PRIMARY KEY,
    holder uuid,
    expires_at timestamptz,
    stream_epoch uuid
);
CREATE UNIQUE INDEX sandbox_activity_stream_epoch ON sandbox_activity_watch (stream_epoch);

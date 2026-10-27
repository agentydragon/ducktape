-- Retain unread output independently of a remote process's lifetime.
ALTER TABLE spike_process ADD COLUMN buffered_output bytea NOT NULL DEFAULT '\x';

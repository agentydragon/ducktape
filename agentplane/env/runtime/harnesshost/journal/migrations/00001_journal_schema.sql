-- +goose Up
CREATE TABLE IF NOT EXISTS event_entry (
    cursor INTEGER NOT NULL PRIMARY KEY,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS command (
    command_id VARCHAR NOT NULL PRIMARY KEY,
    payload BLOB NOT NULL,
    admitted_cursor INTEGER NOT NULL REFERENCES event_entry(cursor),
    dispatch_planned BOOLEAN NOT NULL,
    native_correlation JSON NOT NULL,
    terminal_cursor INTEGER REFERENCES event_entry(cursor)
);
CREATE INDEX IF NOT EXISTS pending_command_admission
    ON command (terminal_cursor, admitted_cursor);
CREATE TABLE IF NOT EXISTS turn_boundary (
    turn_id VARCHAR NOT NULL PRIMARY KEY,
    start_cursor INTEGER NOT NULL,
    end_cursor INTEGER,
    status INTEGER
);
CREATE INDEX IF NOT EXISTS ix_turn_boundary_start_cursor ON turn_boundary (start_cursor);
CREATE TABLE IF NOT EXISTS checkpoint (
    id INTEGER NOT NULL PRIMARY KEY,
    source_id VARCHAR NOT NULL,
    through_cursor INTEGER NOT NULL,
    harness_running BOOLEAN NOT NULL,
    active_turn_id VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS seal (
    id INTEGER NOT NULL PRIMARY KEY,
    cursor INTEGER NOT NULL,
    incarnation_uid VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS debug_checkpoint (
    name VARCHAR NOT NULL,
    command_id VARCHAR NOT NULL,
    PRIMARY KEY (name, command_id)
);

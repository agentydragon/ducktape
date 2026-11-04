-- +goose Up
-- Only a fresh journal or an intact version-3 journal can enter this baseline.
-- The named CHECK constraint makes refusal actionable when SQLite reports the error.
CREATE TEMP TABLE journal_baseline_validation (
    valid INTEGER NOT NULL CONSTRAINT "run previous Runtime image to finish journal migration 3" CHECK (valid = 1)
);
INSERT INTO journal_baseline_validation
WITH
    history AS (
        SELECT
            (SELECT is_applied FROM runner_goose_version WHERE version_id = 1 ORDER BY id DESC LIMIT 1) AS v1,
            (SELECT is_applied FROM runner_goose_version WHERE version_id = 2 ORDER BY id DESC LIMIT 1) AS v2,
            (SELECT is_applied FROM runner_goose_version WHERE version_id = 3 ORDER BY id DESC LIMIT 1) AS v3,
            (SELECT max(version_id) FROM runner_goose_version) AS tip
    ),
    expected_tables(name) AS (VALUES
        ('event_entry'), ('command'), ('turn_boundary'), ('checkpoint'),
        ('seal'), ('debug_checkpoint'), ('native_request'), ('native_request_command')
    ),
    existing_tables AS (
        SELECT count(*) AS total FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name <> 'runner_goose_version'
    ),
    expected_columns(table_name, name, declared_type, required, primary_key) AS (VALUES
        ('event_entry', 'cursor', 'INTEGER', 1, 1),
        ('event_entry', 'payload', 'BLOB', 1, 0),
        ('command', 'command_id', 'VARCHAR', 1, 1),
        ('command', 'payload', 'BLOB', 1, 0),
        ('command', 'admitted_cursor', 'INTEGER', 1, 0),
        ('command', 'dispatch_planned', 'BOOLEAN', 1, 0),
        ('command', 'terminal_cursor', 'INTEGER', 0, 0),
        ('turn_boundary', 'turn_id', 'VARCHAR', 1, 1),
        ('turn_boundary', 'start_cursor', 'INTEGER', 1, 0),
        ('turn_boundary', 'end_cursor', 'INTEGER', 0, 0),
        ('turn_boundary', 'status', 'INTEGER', 0, 0),
        ('checkpoint', 'id', 'INTEGER', 1, 1),
        ('checkpoint', 'source_id', 'VARCHAR', 1, 0),
        ('checkpoint', 'through_cursor', 'INTEGER', 1, 0),
        ('checkpoint', 'harness_running', 'BOOLEAN', 1, 0),
        ('checkpoint', 'active_turn_id', 'VARCHAR', 1, 0),
        ('checkpoint', 'model', 'TEXT', 0, 0),
        ('checkpoint', 'reasoning_effort', 'TEXT', 0, 0),
        ('seal', 'id', 'INTEGER', 1, 1),
        ('seal', 'cursor', 'INTEGER', 1, 0),
        ('seal', 'incarnation_uid', 'VARCHAR', 1, 0),
        ('debug_checkpoint', 'name', 'VARCHAR', 1, 1),
        ('debug_checkpoint', 'command_id', 'VARCHAR', 1, 2),
        ('native_request', 'request_id', 'TEXT', 1, 1),
        ('native_request', 'kind', 'TEXT', 1, 0),
        ('native_request_command', 'request_id', 'TEXT', 1, 1),
        ('native_request_command', 'command_id', 'TEXT', 1, 2)
    ),
    valid_schema AS (
        SELECT
            (SELECT count(*) FROM sqlite_master m JOIN expected_tables e ON e.name = m.name
                WHERE m.type = 'table') = 8
            AND NOT EXISTS (
                SELECT 1 FROM sqlite_master m
                WHERE m.type = 'table' AND m.name NOT LIKE 'sqlite_%'
                    AND m.name <> 'runner_goose_version'
                    AND m.name NOT IN (SELECT name FROM expected_tables)
            )
            AND NOT EXISTS (
                SELECT 1 FROM expected_columns e
                WHERE NOT EXISTS (
                    SELECT 1 FROM pragma_table_info(e.table_name) a
                    WHERE a.name = e.name AND upper(a.type) = e.declared_type
                        AND a."notnull" = e.required AND a.pk = e.primary_key
                )
            )
            AND NOT EXISTS (
                SELECT 1 FROM expected_tables e
                WHERE (SELECT count(*) FROM pragma_table_info(e.name)) <>
                    (SELECT count(*) FROM expected_columns x WHERE x.table_name = e.name)
            ) AS valid
    )
SELECT CASE
    WHEN (SELECT total FROM existing_tables) = 0
        AND (SELECT tip FROM history) = 0
        AND NOT EXISTS (SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'
            AND name <> 'runner_goose_version') THEN 1
    WHEN (SELECT total FROM existing_tables) = 8
        AND (SELECT v1 = 1 AND v2 = 1 AND v3 = 1 AND tip = 3 FROM history)
        AND (SELECT valid FROM valid_schema)
        AND NOT EXISTS (SELECT 1 FROM pragma_integrity_check WHERE integrity_check <> 'ok')
        AND NOT EXISTS (SELECT 1 FROM pragma_foreign_key_check)
        AND EXISTS (SELECT 1 FROM pragma_index_list('command') WHERE name = 'pending_command_admission')
        AND EXISTS (SELECT 1 FROM pragma_index_list('turn_boundary') WHERE name = 'ix_turn_boundary_start_cursor')
        THEN 1
    ELSE 0
END;
DROP TABLE journal_baseline_validation;

CREATE TABLE IF NOT EXISTS event_entry (
    cursor INTEGER NOT NULL PRIMARY KEY,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS command (
    command_id VARCHAR NOT NULL PRIMARY KEY,
    payload BLOB NOT NULL,
    admitted_cursor INTEGER NOT NULL REFERENCES event_entry(cursor),
    dispatch_planned BOOLEAN NOT NULL,
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
    active_turn_id VARCHAR NOT NULL,
    model TEXT,
    reasoning_effort TEXT
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
CREATE TABLE IF NOT EXISTS native_request (
    request_id TEXT NOT NULL PRIMARY KEY CHECK (length(request_id) > 0),
    kind TEXT NOT NULL CHECK (kind IN (
        'claude_submit', 'claude_model_change', 'claude_effort_change', 'codex_turn_start'
    ))
);
CREATE TABLE IF NOT EXISTS native_request_command (
    request_id TEXT NOT NULL REFERENCES native_request(request_id),
    command_id TEXT NOT NULL REFERENCES command(command_id),
    PRIMARY KEY (request_id, command_id),
    UNIQUE (command_id)
);

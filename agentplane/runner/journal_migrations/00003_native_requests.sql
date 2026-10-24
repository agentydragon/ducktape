-- +goose Up
CREATE TABLE native_request (
    request_id TEXT NOT NULL PRIMARY KEY CHECK (length(request_id) > 0),
    kind TEXT NOT NULL CHECK (kind IN (
        'claude_submit', 'claude_model_change', 'claude_effort_change', 'codex_turn_start'
    ))
);
CREATE TABLE native_request_command (
    request_id TEXT NOT NULL REFERENCES native_request(request_id),
    command_id TEXT NOT NULL REFERENCES command(command_id),
    PRIMARY KEY (request_id, command_id),
    UNIQUE (command_id)
);

-- NOT NULL constraints reject unsupported payload shapes before their source column is removed.
CREATE TEMP TABLE native_request_import (
    command_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    model_command_id TEXT,
    effort_command_id TEXT
);
INSERT INTO native_request_import
SELECT command_id,
    CASE json_extract(native_correlation, '$.native_correlation.adapter')
        WHEN 'claude' THEN coalesce(
            json_extract(native_correlation, '$.native_correlation.payload.submit.message_uuid'),
            json_extract(native_correlation, '$.native_correlation.payload.model_change.request_id'),
            json_extract(native_correlation, '$.native_correlation.payload.effort_change.request_id')
        )
        WHEN 'codex' THEN json_extract(native_correlation, '$.native_correlation.payload.turn_start.request_id')
    END,
    CASE json_extract(native_correlation, '$.native_correlation.adapter')
        WHEN 'claude' THEN CASE
            WHEN json_type(native_correlation, '$.native_correlation.payload.submit') = 'object' THEN 'claude_submit'
            WHEN json_type(native_correlation, '$.native_correlation.payload.model_change') = 'object' THEN 'claude_model_change'
            WHEN json_type(native_correlation, '$.native_correlation.payload.effort_change') = 'object' THEN 'claude_effort_change'
        END
        WHEN 'codex' THEN 'codex_turn_start'
    END,
    json_extract(native_correlation, '$.native_correlation.payload.turn_start.model_change.command_id'),
    json_extract(native_correlation, '$.native_correlation.payload.turn_start.effort_change.command_id')
FROM command
WHERE json_type(native_correlation, '$.native_correlation') IS NOT NULL
    AND json_type(native_correlation, '$.native_correlation') != 'null';

INSERT INTO native_request SELECT DISTINCT request_id, kind FROM native_request_import;
INSERT INTO native_request_command
SELECT request_id, command_id FROM native_request_import
UNION ALL
SELECT request_id, model_command_id FROM native_request_import WHERE model_command_id IS NOT NULL
UNION ALL
SELECT request_id, effort_command_id FROM native_request_import WHERE effort_command_id IS NOT NULL;
DROP TABLE native_request_import;

ALTER TABLE command DROP COLUMN native_correlation;

-- +goose Up
-- Reject unrecognized fields rather than discard retained command evidence.
CREATE TEMP TABLE command_correlation_validation (
    valid BOOLEAN NOT NULL CHECK (valid)
);
INSERT INTO command_correlation_validation
SELECT json_type(native_correlation) = 'object'
    AND NOT EXISTS (
        SELECT 1 FROM json_each(native_correlation)
        WHERE type != 'text' OR key NOT IN (
            'harness_message_id', 'turn_id',
            'claude.submit.message_uuid',
            'claude.model.request_id', 'claude.model.value',
            'claude.effort.request_id', 'claude.effort.value',
            'codex.turn_start.request_id',
            'codex.turn_start.model_command_id', 'codex.turn_start.model_value',
            'codex.turn_start.effort_command_id', 'codex.turn_start.effort_value'
        )
    )
FROM command;
DROP TABLE command_correlation_validation;

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

-- Import the deployed correlation map directly into the final relational schema.
-- Request IDs are distinct from command IDs; Codex can link several commands to one request.
CREATE TEMP TABLE native_request_import (
    command_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    model_command_id TEXT,
    effort_command_id TEXT
);
WITH legacy AS (
    SELECT command_id,
        nullif(json_extract(native_correlation, '$."claude.submit.message_uuid"'), '') AS claude_message,
        nullif(json_extract(native_correlation, '$."claude.model.request_id"'), '') AS claude_model,
        nullif(json_extract(native_correlation, '$."claude.effort.request_id"'), '') AS claude_effort,
        nullif(json_extract(native_correlation, '$."codex.turn_start.request_id"'), '') AS codex_request,
        nullif(json_extract(native_correlation, '$."codex.turn_start.model_command_id"'), '') AS codex_model,
        nullif(json_extract(native_correlation, '$."codex.turn_start.effort_command_id"'), '') AS codex_effort
    FROM command
)
INSERT INTO native_request_import
SELECT command_id,
    coalesce(codex_request, claude_message, claude_model, claude_effort),
    CASE
        WHEN codex_request IS NOT NULL THEN 'codex_turn_start'
        WHEN claude_message IS NOT NULL THEN 'claude_submit'
        WHEN claude_model IS NOT NULL THEN 'claude_model_change'
        WHEN claude_effort IS NOT NULL THEN 'claude_effort_change'
    END,
    CASE WHEN codex_request IS NOT NULL THEN codex_model END,
    CASE WHEN codex_request IS NOT NULL THEN codex_effort END
FROM legacy
WHERE coalesce(codex_request, claude_message, claude_model, claude_effort) IS NOT NULL;

INSERT INTO native_request SELECT DISTINCT request_id, kind FROM native_request_import;
INSERT INTO native_request_command
SELECT request_id, command_id FROM native_request_import
UNION ALL
SELECT request_id, model_command_id FROM native_request_import WHERE model_command_id IS NOT NULL
UNION ALL
SELECT request_id, effort_command_id FROM native_request_import WHERE effort_command_id IS NOT NULL;
DROP TABLE native_request_import;

ALTER TABLE command DROP COLUMN native_correlation;

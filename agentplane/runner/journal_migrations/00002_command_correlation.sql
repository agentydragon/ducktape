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

WITH legacy AS (
    SELECT command_id,
        json_extract(native_correlation, '$.harness_message_id') AS message_id,
        json_extract(native_correlation, '$.turn_id') AS turn_id,
        nullif(json_extract(native_correlation, '$."claude.submit.message_uuid"'), '') AS claude_message,
        nullif(json_extract(native_correlation, '$."claude.model.request_id"'), '') AS claude_model,
        json_extract(native_correlation, '$."claude.model.value"') AS claude_model_value,
        nullif(json_extract(native_correlation, '$."claude.effort.request_id"'), '') AS claude_effort,
        json_extract(native_correlation, '$."claude.effort.value"') AS claude_effort_value,
        nullif(json_extract(native_correlation, '$."codex.turn_start.request_id"'), '') AS codex_request,
        nullif(json_extract(native_correlation, '$."codex.turn_start.model_command_id"'), '') AS codex_model,
        json_extract(native_correlation, '$."codex.turn_start.model_value"') AS codex_model_value,
        nullif(json_extract(native_correlation, '$."codex.turn_start.effort_command_id"'), '') AS codex_effort,
        json_extract(native_correlation, '$."codex.turn_start.effort_value"') AS codex_effort_value
    FROM command
), migrated AS (
    SELECT command_id, json_patch('{}', json_object(
        'receipt_metadata', json_object('harness_message_id', message_id, 'turn_id', turn_id),
        'native_correlation', CASE
            WHEN codex_request IS NOT NULL THEN json_object(
                'adapter', 'codex',
                'payload', json_object('turn_start', json_object(
                    'request_id', codex_request,
                    'model_change', CASE WHEN codex_model IS NOT NULL THEN
                        json_object('command_id', codex_model, 'value', codex_model_value) END,
                    'effort_change', CASE WHEN codex_effort IS NOT NULL THEN
                        json_object('command_id', codex_effort, 'value', codex_effort_value) END
                ))
            )
            WHEN claude_message IS NOT NULL OR claude_model IS NOT NULL OR claude_effort IS NOT NULL THEN json_object(
                'adapter', 'claude',
                'payload', json_object(
                    'submit', CASE WHEN claude_message IS NOT NULL THEN
                        json_object('message_uuid', claude_message) END,
                    'model_change', CASE WHEN claude_model IS NOT NULL THEN
                        json_object('request_id', claude_model, 'model', claude_model_value) END,
                    'effort_change', CASE WHEN claude_effort IS NOT NULL THEN
                        json_object('request_id', claude_effort, 'effort', claude_effort_value) END
                )
            )
        END
    )) AS data
    FROM legacy
)
UPDATE command SET native_correlation = (
    SELECT data FROM migrated WHERE migrated.command_id = command.command_id
);

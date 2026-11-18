-- +goose Up
-- Old Dispatcher requests stored a ServiceAccount execution_principal in reserved field 6,
-- or had no execution Identity in field 7. Neither configuration shape is written now.
-- Remove those sampler Sessions and their conversation-owned state before the store stops
-- accepting or exporting them.
-- +goose StatementBegin
CREATE FUNCTION sampler_configuration_is_pre_identity(configuration BYTEA)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $sampler_configuration_is_pre_identity$
DECLARE
    offset_bytes INTEGER := 0;
    tag_value BIGINT;
    field_number BIGINT;
    wire_type BIGINT;
    shift_bits INTEGER;
    byte_value INTEGER;
    item_length BIGINT;
    has_execution_identity BOOLEAN := FALSE;
    has_execution_principal BOOLEAN := FALSE;
BEGIN
    WHILE offset_bytes < octet_length(configuration) LOOP
        tag_value := 0;
        shift_bits := 0;
        LOOP
            IF offset_bytes >= octet_length(configuration) THEN
                RAISE EXCEPTION 'truncated sampler configuration tag';
            END IF;
            byte_value := get_byte(configuration, offset_bytes);
            offset_bytes := offset_bytes + 1;
            tag_value := tag_value | ((byte_value & 127)::BIGINT << shift_bits);
            EXIT WHEN byte_value < 128;
            shift_bits := shift_bits + 7;
            IF shift_bits > 63 THEN
                RAISE EXCEPTION 'oversized sampler configuration tag';
            END IF;
        END LOOP;

        field_number := tag_value >> 3;
        wire_type := tag_value & 7;
        IF field_number = 0 THEN
            RAISE EXCEPTION 'invalid sampler configuration field number';
        END IF;
        IF field_number = 6 THEN
            has_execution_principal := TRUE;
        ELSIF field_number = 7 AND wire_type = 2 THEN
            has_execution_identity := TRUE;
        END IF;

        CASE wire_type
            WHEN 0 THEN
                LOOP
                    IF offset_bytes >= octet_length(configuration) THEN
                        RAISE EXCEPTION 'truncated sampler configuration varint';
                    END IF;
                    byte_value := get_byte(configuration, offset_bytes);
                    offset_bytes := offset_bytes + 1;
                    EXIT WHEN byte_value < 128;
                END LOOP;
            WHEN 1 THEN
                offset_bytes := offset_bytes + 8;
            WHEN 2 THEN
                item_length := 0;
                shift_bits := 0;
                LOOP
                    IF offset_bytes >= octet_length(configuration) THEN
                        RAISE EXCEPTION 'truncated sampler configuration length';
                    END IF;
                    byte_value := get_byte(configuration, offset_bytes);
                    offset_bytes := offset_bytes + 1;
                    item_length := item_length | ((byte_value & 127)::BIGINT << shift_bits);
                    EXIT WHEN byte_value < 128;
                    shift_bits := shift_bits + 7;
                    IF shift_bits > 63 THEN
                        RAISE EXCEPTION 'oversized sampler configuration length';
                    END IF;
                END LOOP;
                offset_bytes := offset_bytes + item_length::INTEGER;
            WHEN 5 THEN
                offset_bytes := offset_bytes + 4;
            ELSE
                RAISE EXCEPTION 'unsupported sampler configuration wire type %', wire_type;
        END CASE;
        IF offset_bytes > octet_length(configuration) THEN
            RAISE EXCEPTION 'sampler configuration field exceeds its byte length';
        END IF;
    END LOOP;

    RETURN has_execution_principal OR NOT has_execution_identity;
END
$sampler_configuration_is_pre_identity$;

CREATE TEMP TABLE pre_identity_sampler_session ON COMMIT DROP AS
SELECT id, conversation_id
FROM sampler_session
WHERE sampler_configuration_is_pre_identity(configuration);

DELETE FROM sampler_command WHERE session_id IN (SELECT id FROM pre_identity_sampler_session);
DELETE FROM sampler_event WHERE session_id IN (SELECT id FROM pre_identity_sampler_session);
DELETE FROM sampler_export_checkpoint WHERE session_id IN (SELECT id FROM pre_identity_sampler_session);
DELETE FROM sampler_notification WHERE conversation_id IN (SELECT conversation_id FROM pre_identity_sampler_session);
DELETE FROM sampler_model_request WHERE conversation_id IN (SELECT conversation_id FROM pre_identity_sampler_session);
DELETE FROM sampler_intent WHERE conversation_id IN (SELECT conversation_id FROM pre_identity_sampler_session);
DELETE FROM sampler_process WHERE conversation_id IN (SELECT conversation_id FROM pre_identity_sampler_session);
DELETE FROM sampler_cell WHERE conversation_id IN (SELECT conversation_id FROM pre_identity_sampler_session);
DELETE FROM sampler_interrupt WHERE conversation_id IN (SELECT conversation_id FROM pre_identity_sampler_session);
DELETE FROM sampler_session WHERE id IN (SELECT id FROM pre_identity_sampler_session);
DELETE FROM sampler_conversation WHERE id IN (SELECT conversation_id FROM pre_identity_sampler_session);

DROP FUNCTION sampler_configuration_is_pre_identity(BYTEA);
-- +goose StatementEnd

-- +goose Down
-- +goose StatementBegin
DO $$ BEGIN RAISE EXCEPTION 'pre-Identity Session data cannot be restored'; END $$;
-- +goose StatementEnd

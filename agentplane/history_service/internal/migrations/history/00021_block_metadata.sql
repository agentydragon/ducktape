-- +goose Up
-- Nullable columns leave existing blocks in place. The online backfill can be resumed.
ALTER TABLE session_event_block ADD COLUMN block_format text, ADD COLUMN uncompressed_bytes bigint;

-- +goose StatementBegin
CREATE FUNCTION session_event_block_set_metadata() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    prefix bytea := substring(NEW.payload FROM 1 FOR 8);
    trailer bytea;
BEGIN
    NEW.block_format := 'unknown';
    NEW.uncompressed_bytes := NULL;
    IF octet_length(prefix) = 8 AND substring(prefix FROM 1 FOR 4) = decode('41504201', 'hex') THEN
        NEW.block_format := 'apb_v1';
        NEW.uncompressed_bytes := (get_byte(prefix, 4)::bigint << 24) + (get_byte(prefix, 5) << 16)
                                + (get_byte(prefix, 6) << 8) + get_byte(prefix, 7);
    ELSIF substring(prefix FROM 1 FOR 3) = decode('1f8b08', 'hex') AND octet_length(NEW.payload) >= 18 THEN
        -- The legacy writers emit a single gzip member. Its bounded size fits in ISIZE.
        trailer := substring(NEW.payload FROM octet_length(NEW.payload) - 3 FOR 4);
        NEW.block_format := 'legacy_gzip';
        NEW.uncompressed_bytes := (get_byte(trailer, 3)::bigint << 24) + (get_byte(trailer, 2) << 16)
                                + (get_byte(trailer, 1) << 8) + get_byte(trailer, 0);
    END IF;
    RETURN NEW;
END $$;
-- +goose StatementEnd

CREATE TRIGGER set_block_metadata BEFORE INSERT OR UPDATE OF payload, block_format, uncompressed_bytes ON session_event_block
    FOR EACH ROW EXECUTE FUNCTION session_event_block_set_metadata();

-- +goose Down
DROP TRIGGER set_block_metadata ON session_event_block;
DROP FUNCTION session_event_block_set_metadata();
ALTER TABLE session_event_block DROP COLUMN block_format, DROP COLUMN uncompressed_bytes;

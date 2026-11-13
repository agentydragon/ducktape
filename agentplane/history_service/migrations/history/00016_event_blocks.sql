-- +goose Up
-- Only new writes made with compaction enabled enter this index. Existing rows and oversized
-- frames cannot turn each append into a scan of an ever-growing unpacked prefix.
ALTER TABLE session_event ADD COLUMN compactable BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX ix_session_event_compactable_cursor ON session_event (session_id, kind, cursor) WHERE compactable;

CREATE TABLE session_event_block (
    session_id UUID NOT NULL REFERENCES session_history (id),
    first_cursor BIGINT NOT NULL,
    last_cursor BIGINT NOT NULL,
    kind INTEGER NOT NULL,
    cursors BIGINT[] NOT NULL,
    sizes INTEGER[] NOT NULL,
    payload BYTEA NOT NULL,
    PRIMARY KEY (session_id, first_cursor),
    CHECK (first_cursor > 0 AND last_cursor >= first_cursor),
    CHECK (cardinality(cursors) BETWEEN 1 AND 256),
    CHECK (cardinality(cursors) = cardinality(sizes)),
    CHECK (cursors[1] = first_cursor AND cursors[cardinality(cursors)] = last_cursor)
);
CREATE INDEX ix_session_event_block_kind_first ON session_event_block (session_id, kind, first_cursor);
CREATE INDEX ix_session_event_block_kind_last ON session_event_block (session_id, kind, last_cursor);

-- +goose Down
-- Packed events cannot be discarded by a schema rollback. Deploy readers for this schema first.
-- +goose StatementBegin
DO $$ BEGIN RAISE EXCEPTION 'event blocks require a lossless unpack before downgrade'; END $$;
-- +goose StatementEnd

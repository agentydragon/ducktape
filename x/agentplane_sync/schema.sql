CREATE TABLE projection_checkpoint (
  conversation_id text PRIMARY KEY,
  source_id text NOT NULL,
  through_cursor bigint NOT NULL CHECK (through_cursor >= 0)
);

CREATE TABLE projection_event (
  conversation_id text NOT NULL,
  source_id text NOT NULL,
  source_cursor bigint NOT NULL CHECK (source_cursor > 0),
  digest bytea NOT NULL,
  PRIMARY KEY (conversation_id, source_id, source_cursor)
);

CREATE TABLE sync_view_row (
  conversation_id text NOT NULL,
  row_key text NOT NULL,
  entity_kind text NOT NULL,
  anchor bigint NOT NULL CHECK (anchor >= 0),
  revision bigint NOT NULL CHECK (revision >= anchor),
  item_id text,
  item_kind integer,
  tool_name text,
  text_revision bigint NOT NULL DEFAULT 0,
  arguments_revision bigint NOT NULL DEFAULT 0,
  output_revision bigint NOT NULL DEFAULT 0,
  reasoning_revision bigint NOT NULL DEFAULT 0,
  text_bytes bigint NOT NULL DEFAULT 0,
  arguments_bytes bigint NOT NULL DEFAULT 0,
  output_bytes bigint NOT NULL DEFAULT 0,
  reasoning_bytes bigint NOT NULL DEFAULT 0,
  status text,
  model text,
  command_id text,
  PRIMARY KEY (conversation_id, row_key)
);

CREATE INDEX sync_view_row_tail
  ON sync_view_row (conversation_id, anchor DESC, row_key);

CREATE TABLE projected_payload_part (
  conversation_id text NOT NULL,
  item_id text NOT NULL,
  field_name text NOT NULL CHECK (field_name IN ('text', 'arguments', 'output', 'reasoning')),
  source_cursor bigint NOT NULL CHECK (source_cursor > 0),
  operation text NOT NULL CHECK (operation IN ('append', 'replace')),
  content text NOT NULL,
  PRIMARY KEY (conversation_id, item_id, field_name, source_cursor)
);

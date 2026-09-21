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
  text_payload_ref text,
  arguments_payload_ref text,
  output_payload_ref text,
  reasoning_payload_ref text,
  text_generation_id text,
  arguments_generation_id text,
  output_generation_id text,
  reasoning_generation_id text,
  text_chunk_count integer NOT NULL DEFAULT 0 CHECK (text_chunk_count >= 0),
  arguments_chunk_count integer NOT NULL DEFAULT 0 CHECK (arguments_chunk_count >= 0),
  output_chunk_count integer NOT NULL DEFAULT 0 CHECK (output_chunk_count >= 0),
  reasoning_chunk_count integer NOT NULL DEFAULT 0 CHECK (reasoning_chunk_count >= 0),
  status text,
  model text,
  command_id text,
  PRIMARY KEY (conversation_id, row_key)
);

CREATE INDEX sync_view_row_tail
  ON sync_view_row (conversation_id, anchor DESC, row_key);

CREATE TABLE projected_payload_manifest (
  payload_ref text PRIMARY KEY,
  conversation_id text NOT NULL,
  item_id text NOT NULL,
  field_name text NOT NULL CHECK (field_name IN ('text', 'arguments', 'output', 'reasoning')),
  source_id text NOT NULL,
  generation_id text NOT NULL,
  revision bigint NOT NULL CHECK (revision > 0),
  present boolean NOT NULL CHECK (present),
  chunk_count integer NOT NULL CHECK (chunk_count >= 0),
  content_bytes bigint NOT NULL CHECK (content_bytes >= 0),
  source_cursor bigint NOT NULL CHECK (source_cursor > 0),
  UNIQUE (conversation_id, item_id, field_name, source_id, revision)
);

CREATE INDEX projected_payload_manifest_owner
  ON projected_payload_manifest (conversation_id, item_id, field_name, source_id, generation_id, revision DESC);

CREATE TABLE projected_payload_chunk (
  conversation_id text NOT NULL,
  item_id text NOT NULL,
  field_name text NOT NULL CHECK (field_name IN ('text', 'arguments', 'output', 'reasoning')),
  source_id text NOT NULL,
  generation_id text NOT NULL,
  chunk_index integer NOT NULL CHECK (chunk_index >= 0),
  source_cursor bigint NOT NULL CHECK (source_cursor > 0),
  content text NOT NULL,
  content_bytes bigint NOT NULL CHECK (content_bytes >= 0),
  PRIMARY KEY (conversation_id, item_id, field_name, source_id, generation_id, chunk_index)
);

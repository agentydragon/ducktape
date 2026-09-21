"""Small transactional projection of Agentplane's durable EventEntry messages."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from agentplane.protocol import event_pb2, event_log_pb2

POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807
_SCHEMA = Path(__file__).with_name("schema.sql").read_text()
_ROW_FIELDS = (
    "conversation_id",
    "row_key",
    "entity_kind",
    "anchor",
    "revision",
    "item_id",
    "item_kind",
    "tool_name",
    "text_revision",
    "arguments_revision",
    "output_revision",
    "reasoning_revision",
    "text_bytes",
    "arguments_bytes",
    "output_bytes",
    "reasoning_bytes",
    "status",
    "model",
    "command_id",
)
_UPSERT_ROW = """
INSERT INTO sync_view_row (
  conversation_id, row_key, entity_kind, anchor, revision, item_id, item_kind, tool_name,
  text_revision, arguments_revision, output_revision, reasoning_revision,
  text_bytes, arguments_bytes, output_bytes, reasoning_bytes, status, model, command_id
) VALUES (
  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19
)
ON CONFLICT (conversation_id, row_key) DO UPDATE SET
  entity_kind = EXCLUDED.entity_kind,
  anchor = sync_view_row.anchor,
  revision = EXCLUDED.revision,
  item_id = EXCLUDED.item_id,
  item_kind = EXCLUDED.item_kind,
  tool_name = EXCLUDED.tool_name,
  text_revision = EXCLUDED.text_revision,
  arguments_revision = EXCLUDED.arguments_revision,
  output_revision = EXCLUDED.output_revision,
  reasoning_revision = EXCLUDED.reasoning_revision,
  text_bytes = EXCLUDED.text_bytes,
  arguments_bytes = EXCLUDED.arguments_bytes,
  output_bytes = EXCLUDED.output_bytes,
  reasoning_bytes = EXCLUDED.reasoning_bytes,
  status = EXCLUDED.status,
  model = EXCLUDED.model,
  command_id = EXCLUDED.command_id
"""


class ProjectionGap(ValueError):
    """A source batch skipped a cursor already required by the projection."""


class ProjectionConflict(ValueError):
    """A previously applied source cursor was reused with different content."""


class UnknownItem(ValueError):
    """A semantic update referenced an item that has no start row."""


@dataclass(frozen=True)
class ApplyResult:
    through_cursor: int
    duplicate_events: int
    row_writes: int
    payload_parts: int


async def initialize_database(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection:
        await connection.execute(_SCHEMA)


def _row_keys(entry: event_log_pb2.EventEntry) -> set[str]:
    event = entry.event
    case = event.WhichOneof("observation")
    if case in {"item_started", "text_delta", "tool_arguments_delta", "tool_arguments", "tool_output_delta", "item_completed"}:
        return {f"item:{getattr(event, case).item_id}"}
    if case == "command_admitted":
        return {f"command:{event.command_admitted.command.command_id}"}
    if case in {"command_failed", "command_noop"}:
        return {f"command:{getattr(event, case).command_id}"}
    if case == "model_changed":
        keys = {"control:model"}
        if event.model_changed.command_id:
            keys.add(f"command:{event.model_changed.command_id}")
        return keys
    if case in {"turn_started", "turn_completed"}:
        return {f"turn:{getattr(event, case).turn_id}"}
    return set()


def _new_row(conversation_id: str, row_key: str, entity_kind: str, cursor: int) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "row_key": row_key,
        "entity_kind": entity_kind,
        "anchor": cursor,
        "revision": cursor,
        "item_id": None,
        "item_kind": None,
        "tool_name": None,
        "text_revision": 0,
        "arguments_revision": 0,
        "output_revision": 0,
        "reasoning_revision": 0,
        "text_bytes": 0,
        "arguments_bytes": 0,
        "output_bytes": 0,
        "reasoning_bytes": 0,
        "status": None,
        "model": None,
        "command_id": None,
    }


def _payload(
    conversation_id: str,
    item_id: str,
    field_name: str,
    cursor: int,
    operation: str,
    content: str,
) -> tuple[str, str, str, int, str, str]:
    return conversation_id, item_id, field_name, cursor, operation, content


def _digest(entry: event_log_pb2.EventEntry) -> bytes:
    return hashlib.sha256(entry.SerializeToString(deterministic=True)).digest()


def _append_payload(
    row: dict[str, Any],
    payload_parts: list[tuple[str, str, str, int, str, str]],
    field_name: str,
    cursor: int,
    content: str,
    operation: str = "append",
) -> None:
    payload_parts.append(_payload(row["conversation_id"], row["item_id"], field_name, cursor, operation, content))
    version_column = f"{field_name}_revision"
    bytes_column = f"{field_name}_bytes"
    row[version_column] = cursor
    byte_count = len(content.encode("utf-8"))
    row[bytes_column] = row[bytes_column] + byte_count if operation == "append" else byte_count


def _apply_event(
    conversation_id: str,
    rows: dict[str, dict[str, Any]],
    payload_parts: list[tuple[str, str, str, int, str, str]],
    entry: event_log_pb2.EventEntry,
) -> None:
    event = entry.event
    cursor = entry.cursor
    case = event.WhichOneof("observation")

    def row_for(key: str, kind: str, *, create: bool = False) -> dict[str, Any]:
        row = rows.get(key)
        if row is None:
            if not create:
                raise UnknownItem(f"{key} has no projected start row at cursor {cursor}")
            row = _new_row(conversation_id, key, kind, cursor)
            rows[key] = row
        elif row["entity_kind"] != kind:
            raise ProjectionConflict(f"{key} changed entity kind from {row['entity_kind']} to {kind}")
        row["revision"] = cursor
        return row

    if case == "item_started":
        value = event.item_started
        key = f"item:{value.item_id}"
        row = row_for(key, "item", create=True)
        row["item_id"] = value.item_id
        row["item_kind"] = value.kind
        row["tool_name"] = value.tool_name or None
        row["status"] = "streaming"
    elif case in {"text_delta", "tool_arguments_delta", "tool_arguments", "tool_output_delta"}:
        value = getattr(event, case)
        row = row_for(f"item:{value.item_id}", "item")
        row["item_id"] = value.item_id
        if case == "text_delta":
            field_name = "reasoning" if row["item_kind"] == event_pb2.ITEM_KIND_REASONING else "text"
            _append_payload(row, payload_parts, field_name, cursor, value.text)
        elif case == "tool_arguments_delta":
            _append_payload(row, payload_parts, "arguments", cursor, value.partial_json)
        elif case == "tool_arguments":
            _append_payload(row, payload_parts, "arguments", cursor, value.arguments_json, "replace")
        else:
            _append_payload(row, payload_parts, "output", cursor, value.text)
    elif case == "item_completed":
        value = event.item_completed
        row = row_for(f"item:{value.item_id}", "item")
        outcome = value.WhichOneof("outcome")
        if outcome == "text":
            field_name = "reasoning" if row["item_kind"] == event_pb2.ITEM_KIND_REASONING else "text"
            _append_payload(row, payload_parts, field_name, cursor, value.text, "replace")
            row["status"] = "complete"
        elif outcome == "tool":
            _append_payload(row, payload_parts, "output", cursor, value.tool.output, "replace")
            row["status"] = "tool_succeeded" if value.tool.succeeded else "tool_failed"
        else:
            row["status"] = "complete"
    elif case == "command_admitted":
        command_id = event.command_admitted.command.command_id
        row = row_for(f"command:{command_id}", "command", create=True)
        row["command_id"] = command_id
        row["status"] = "pending"
    elif case in {"command_failed", "command_noop"}:
        value = getattr(event, case)
        row = row_for(f"command:{value.command_id}", "command", create=True)
        row["command_id"] = value.command_id
        row["status"] = "failed" if case == "command_failed" else "noop"
    elif case == "model_changed":
        value = event.model_changed
        row = row_for("control:model", "control", create=True)
        row["model"] = value.model
        row["status"] = "current"
        if value.command_id:
            command = row_for(f"command:{value.command_id}", "command", create=True)
            command["command_id"] = value.command_id
            command["status"] = "applied"
    elif case == "turn_started":
        value = event.turn_started
        row = row_for(f"turn:{value.turn_id}", "turn", create=True)
        row["status"] = "active"
        row["model"] = value.model
    elif case == "turn_completed":
        value = event.turn_completed
        row = row_for(f"turn:{value.turn_id}", "turn", create=True)
        row["status"] = str(value.status)
    else:
        return


async def apply_batch(
    pool: asyncpg.Pool,
    *,
    conversation_id: str,
    source_id: str,
    entries: list[event_log_pb2.EventEntry],
) -> ApplyResult:
    if not entries:
        raise ValueError("A projection batch must contain at least one EventEntry")
    cursors = [entry.cursor for entry in entries]
    if cursors != sorted(set(cursors)):
        raise ValueError("EventEntry cursors must be strictly increasing")
    for entry in entries:
        if entry.cursor <= 0 or entry.cursor > POSTGRES_BIGINT_MAX:
            raise ValueError(f"Cursor {entry.cursor} is outside PostgreSQL bigint range")
        if entry.origin.source_id != source_id or entry.origin.sequence != entry.cursor:
            raise ValueError("EventEntry origin must match the original source cursor")

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """INSERT INTO projection_checkpoint (conversation_id, source_id, through_cursor)
               VALUES ($1, $2, 0) ON CONFLICT (conversation_id) DO NOTHING""",
            conversation_id,
            source_id,
        )
        checkpoint = await connection.fetchrow(
            "SELECT source_id, through_cursor FROM projection_checkpoint WHERE conversation_id = $1 FOR UPDATE",
            conversation_id,
        )
        if checkpoint["source_id"] != source_id:
            raise ProjectionConflict(f"Conversation {conversation_id} is already owned by source {checkpoint['source_id']}")
        through = int(checkpoint["through_cursor"])
        original_through = through
        duplicate_events = 0
        new_entries: list[event_log_pb2.EventEntry] = []

        for entry in entries:
            cursor = int(entry.cursor)
            digest = _digest(entry)
            if cursor <= through:
                prior = await connection.fetchval(
                    """SELECT digest FROM projection_event
                       WHERE conversation_id = $1 AND source_id = $2 AND source_cursor = $3""",
                    conversation_id,
                    source_id,
                    cursor,
                )
                if prior is None or bytes(prior) != digest:
                    raise ProjectionConflict(f"Cursor {cursor} was already covered with different or missing evidence")
                duplicate_events += 1
                continue
            if cursor != through + 1:
                raise ProjectionGap(f"Expected source cursor {through + 1}, got {cursor}")
            await connection.execute(
                """INSERT INTO projection_event (conversation_id, source_id, source_cursor, digest)
                   VALUES ($1, $2, $3, $4)""",
                conversation_id,
                source_id,
                cursor,
                digest,
            )
            new_entries.append(entry)
            through = cursor

        if not new_entries:
            return ApplyResult(through, duplicate_events, 0, 0)

        keys = set().union(*(_row_keys(entry) for entry in new_entries))
        existing_rows = await connection.fetch(
            "SELECT * FROM sync_view_row WHERE conversation_id = $1 AND row_key = ANY($2::text[])",
            conversation_id,
            sorted(keys),
        )
        rows = {record["row_key"]: dict(record) for record in existing_rows}
        payload_parts: list[tuple[str, str, str, int, str, str]] = []
        for entry in new_entries:
            _apply_event(conversation_id, rows, payload_parts, entry)

        if payload_parts:
            await connection.executemany(
                """INSERT INTO projected_payload_part
                   (conversation_id, item_id, field_name, source_cursor, operation, content)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                payload_parts,
            )
        if rows:
            await connection.executemany(
                _UPSERT_ROW,
                [tuple(row[field] for field in _ROW_FIELDS) for row in rows.values()],
            )
        await connection.execute(
            "UPDATE projection_checkpoint SET through_cursor = $2 WHERE conversation_id = $1",
            conversation_id,
            through,
        )
        return ApplyResult(through, duplicate_events, len(rows), len(payload_parts))

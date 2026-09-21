"""Small transactional projection of Agentplane's durable EventEntry messages."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from agentplane.protocol import event_log_pb2, event_pb2

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
    "text_payload_ref",
    "arguments_payload_ref",
    "output_payload_ref",
    "reasoning_payload_ref",
    "text_generation_id",
    "arguments_generation_id",
    "output_generation_id",
    "reasoning_generation_id",
    "text_chunk_count",
    "arguments_chunk_count",
    "output_chunk_count",
    "reasoning_chunk_count",
    "status",
    "model",
    "command_id",
)
_UPSERT_ROW = """
INSERT INTO sync_view_row (
  conversation_id, row_key, entity_kind, anchor, revision, item_id, item_kind, tool_name,
  text_revision, arguments_revision, output_revision, reasoning_revision,
  text_bytes, arguments_bytes, output_bytes, reasoning_bytes,
  text_payload_ref, arguments_payload_ref, output_payload_ref, reasoning_payload_ref,
  text_generation_id, arguments_generation_id, output_generation_id, reasoning_generation_id,
  text_chunk_count, arguments_chunk_count, output_chunk_count, reasoning_chunk_count,
  status, model, command_id
) VALUES (
  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19,
  $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30, $31
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
  text_payload_ref = EXCLUDED.text_payload_ref,
  arguments_payload_ref = EXCLUDED.arguments_payload_ref,
  output_payload_ref = EXCLUDED.output_payload_ref,
  reasoning_payload_ref = EXCLUDED.reasoning_payload_ref,
  text_generation_id = EXCLUDED.text_generation_id,
  arguments_generation_id = EXCLUDED.arguments_generation_id,
  output_generation_id = EXCLUDED.output_generation_id,
  reasoning_generation_id = EXCLUDED.reasoning_generation_id,
  text_chunk_count = EXCLUDED.text_chunk_count,
  arguments_chunk_count = EXCLUDED.arguments_chunk_count,
  output_chunk_count = EXCLUDED.output_chunk_count,
  reasoning_chunk_count = EXCLUDED.reasoning_chunk_count,
  status = EXCLUDED.status,
  model = EXCLUDED.model,
  command_id = EXCLUDED.command_id
"""


class ProjectionGapError(ValueError):
    """A source batch skipped a cursor already required by the projection."""


class ProjectionConflictError(ValueError):
    """A previously applied source cursor was reused with different content."""


class UnknownItemError(ValueError):
    """A semantic update referenced an item that has no start row."""


@dataclass(frozen=True)
class ApplyResult:
    through_cursor: int
    duplicate_events: int
    row_writes: int
    payload_parts: int


@dataclass(frozen=True)
class PayloadState:
    source_id: str
    generation_id: int
    revision: int
    chunk_count: int
    content_bytes: int


def payload_reference(
    conversation_id: str, item_id: str, field_name: str, source_id: str, generation_id: int, revision: int
) -> str:
    identity = json.dumps(
        [conversation_id, item_id, field_name, source_id, generation_id, revision],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return f"pr1_{base64.urlsafe_b64encode(identity).decode().rstrip('=')}"


async def initialize_database(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection:
        await connection.execute(_SCHEMA)


def _row_keys(entry: event_log_pb2.EventEntry) -> set[str]:
    event = entry.event
    case = event.WhichOneof("observation")
    if case in {
        "item_started",
        "text_delta",
        "tool_arguments_delta",
        "tool_arguments",
        "tool_output_delta",
        "item_completed",
    }:
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
        "text_payload_ref": None,
        "arguments_payload_ref": None,
        "output_payload_ref": None,
        "reasoning_payload_ref": None,
        "text_generation_id": None,
        "arguments_generation_id": None,
        "output_generation_id": None,
        "reasoning_generation_id": None,
        "text_chunk_count": 0,
        "arguments_chunk_count": 0,
        "output_chunk_count": 0,
        "reasoning_chunk_count": 0,
        "status": None,
        "model": None,
        "command_id": None,
    }


def _digest(entry: event_log_pb2.EventEntry) -> bytes:
    return hashlib.sha256(entry.SerializeToString(deterministic=True)).digest()


def _append_payload(
    row: dict[str, Any],
    payload_manifests: list[tuple[str, str, str, str, str, int, int, bool, int, int, int]],
    payload_chunks: list[tuple[str, str, str, str, int, int, int, str, int]],
    payload_states: dict[tuple[str, str], PayloadState],
    source_id: str,
    field_name: str,
    cursor: int,
    content: str,
    operation: str = "append",
) -> None:
    if operation not in {"append", "replace"}:
        raise ValueError(f"Unknown payload operation {operation}")
    conversation_id = row["conversation_id"]
    item_id = row["item_id"]
    owner = (item_id, field_name)
    previous = payload_states.get(owner)
    if operation == "replace" or previous is None:
        generation_id = cursor
        chunk_count = 0
        content_bytes = 0
    else:
        if previous.source_id != source_id:
            raise ProjectionConflictError(f"{owner} payload source changed from {previous.source_id} to {source_id}")
        if cursor <= previous.revision:
            raise ProjectionConflictError(f"{owner} payload cursor did not advance")
        generation_id = previous.generation_id
        chunk_count = previous.chunk_count
        content_bytes = previous.content_bytes
    encoded = content.encode("utf-8")
    if encoded:
        payload_chunks.append(
            (conversation_id, item_id, field_name, source_id, generation_id, chunk_count, cursor, content, len(encoded))
        )
        chunk_count += 1
    content_bytes = content_bytes + len(encoded) if operation == "append" and previous is not None else len(encoded)
    payload_ref = payload_reference(conversation_id, item_id, field_name, source_id, generation_id, cursor)
    payload_manifests.append(
        (
            payload_ref,
            conversation_id,
            item_id,
            field_name,
            source_id,
            generation_id,
            cursor,
            True,
            chunk_count,
            content_bytes,
            cursor,
        )
    )
    payload_states[owner] = PayloadState(source_id, generation_id, cursor, chunk_count, content_bytes)
    version_column = f"{field_name}_revision"
    bytes_column = f"{field_name}_bytes"
    row[f"{field_name}_payload_ref"] = payload_ref
    row[f"{field_name}_generation_id"] = generation_id
    row[f"{field_name}_chunk_count"] = chunk_count
    row[version_column] = cursor
    row[bytes_column] = content_bytes


def _apply_event(
    conversation_id: str,
    source_id: str,
    rows: dict[str, dict[str, Any]],
    payload_manifests: list[tuple[str, str, str, str, str, int, int, bool, int, int, int]],
    payload_chunks: list[tuple[str, str, str, str, int, int, int, str, int]],
    payload_states: dict[tuple[str, str], PayloadState],
    entry: event_log_pb2.EventEntry,
) -> None:
    event = entry.event
    cursor = entry.cursor
    case = event.WhichOneof("observation")

    def row_for(key: str, kind: str, *, create: bool = False) -> dict[str, Any]:
        row = rows.get(key)
        if row is None:
            if not create:
                raise UnknownItemError(f"{key} has no projected start row at cursor {cursor}")
            row = _new_row(conversation_id, key, kind, cursor)
            rows[key] = row
        elif row["entity_kind"] != kind:
            raise ProjectionConflictError(f"{key} changed entity kind from {row['entity_kind']} to {kind}")
        row["revision"] = cursor
        return row

    if case == "item_started":
        started = event.item_started
        key = f"item:{started.item_id}"
        row = row_for(key, "item", create=True)
        row["item_id"] = started.item_id
        row["item_kind"] = started.kind
        row["tool_name"] = started.tool_name or None
        row["status"] = "streaming"
    elif case in {"text_delta", "tool_arguments_delta", "tool_arguments", "tool_output_delta"}:
        delta = getattr(event, case)
        row = row_for(f"item:{delta.item_id}", "item")
        row["item_id"] = delta.item_id
        if case == "text_delta":
            field_name = "reasoning" if row["item_kind"] == event_pb2.ITEM_KIND_REASONING else "text"
            _append_payload(
                row, payload_manifests, payload_chunks, payload_states, source_id, field_name, cursor, delta.text
            )
        elif case == "tool_arguments_delta":
            _append_payload(
                row,
                payload_manifests,
                payload_chunks,
                payload_states,
                source_id,
                "arguments",
                cursor,
                delta.partial_json,
            )
        elif case == "tool_arguments":
            _append_payload(
                row,
                payload_manifests,
                payload_chunks,
                payload_states,
                source_id,
                "arguments",
                cursor,
                delta.arguments_json,
                "replace",
            )
        else:
            _append_payload(
                row, payload_manifests, payload_chunks, payload_states, source_id, "output", cursor, delta.text
            )
    elif case == "item_completed":
        completed = event.item_completed
        row = row_for(f"item:{completed.item_id}", "item")
        outcome = completed.WhichOneof("outcome")
        if outcome == "text":
            field_name = "reasoning" if row["item_kind"] == event_pb2.ITEM_KIND_REASONING else "text"
            _append_payload(
                row,
                payload_manifests,
                payload_chunks,
                payload_states,
                source_id,
                field_name,
                cursor,
                completed.text,
                "replace",
            )
            row["status"] = "complete"
        elif outcome == "tool":
            _append_payload(
                row,
                payload_manifests,
                payload_chunks,
                payload_states,
                source_id,
                "output",
                cursor,
                completed.tool.output,
                "replace",
            )
            row["status"] = "tool_succeeded" if completed.tool.succeeded else "tool_failed"
        else:
            row["status"] = "complete"
    elif case == "command_admitted":
        admitted_command = event.command_admitted.command.command_id
        command_id = admitted_command
        row = row_for(f"command:{command_id}", "command", create=True)
        row["command_id"] = command_id
        row["status"] = "pending"
    elif case in {"command_failed", "command_noop"}:
        command_update = getattr(event, case)
        row = row_for(f"command:{command_update.command_id}", "command", create=True)
        row["command_id"] = command_update.command_id
        row["status"] = "failed" if case == "command_failed" else "noop"
    elif case == "model_changed":
        model_change = event.model_changed
        row = row_for("control:model", "control", create=True)
        row["model"] = model_change.model
        row["status"] = "current"
        if model_change.command_id:
            command = row_for(f"command:{model_change.command_id}", "command", create=True)
            command["command_id"] = model_change.command_id
            command["status"] = "applied"
    elif case == "turn_started":
        turn_started = event.turn_started
        row = row_for(f"turn:{turn_started.turn_id}", "turn", create=True)
        row["status"] = "active"
        row["model"] = turn_started.model
    elif case == "turn_completed":
        turn_completed = event.turn_completed
        row = row_for(f"turn:{turn_completed.turn_id}", "turn", create=True)
        row["status"] = str(turn_completed.status)
    else:
        return


async def apply_batch(
    pool: asyncpg.Pool, *, conversation_id: str, source_id: str, entries: list[event_log_pb2.EventEntry]
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
        if checkpoint is None:
            raise ProjectionConflictError(f"Conversation {conversation_id} has no projection checkpoint")
        if checkpoint["source_id"] != source_id:
            raise ProjectionConflictError(
                f"Conversation {conversation_id} is already owned by source {checkpoint['source_id']}"
            )
        through = int(checkpoint["through_cursor"])
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
                    raise ProjectionConflictError(
                        f"Cursor {cursor} was already covered with different or missing evidence"
                    )
                duplicate_events += 1
                continue
            if cursor != through + 1:
                raise ProjectionGapError(f"Expected source cursor {through + 1}, got {cursor}")
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
        payload_refs = {
            row[f"{field_name}_payload_ref"]
            for row in rows.values()
            for field_name in ("text", "arguments", "output", "reasoning")
            if row[f"{field_name}_payload_ref"] is not None
        }
        payload_states: dict[tuple[str, str], PayloadState] = {}
        if payload_refs:
            manifests = await connection.fetch(
                """SELECT payload_ref, item_id, field_name, source_id, generation_id, revision,
                          chunk_count, content_bytes
                   FROM projected_payload_manifest WHERE payload_ref = ANY($1::text[])""",
                list(payload_refs),
            )
            by_ref = {
                record["payload_ref"]: PayloadState(
                    record["source_id"],
                    record["generation_id"],
                    int(record["revision"]),
                    record["chunk_count"],
                    int(record["content_bytes"]),
                )
                for record in manifests
            }
            if set(by_ref) != payload_refs:
                raise ProjectionConflictError("A current payload reference has no immutable manifest")
            for row in rows.values():
                if row["item_id"] is None:
                    continue
                for field_name in ("text", "arguments", "output", "reasoning"):
                    payload_ref = row[f"{field_name}_payload_ref"]
                    if payload_ref is not None:
                        payload_states[(row["item_id"], field_name)] = by_ref[payload_ref]
        payload_manifests: list[tuple[str, str, str, str, str, int, int, bool, int, int, int]] = []
        payload_chunks: list[tuple[str, str, str, str, int, int, int, str, int]] = []
        for entry in new_entries:
            _apply_event(conversation_id, source_id, rows, payload_manifests, payload_chunks, payload_states, entry)

        if payload_chunks:
            await connection.executemany(
                """INSERT INTO projected_payload_chunk
                   (conversation_id, item_id, field_name, source_id, generation_id, chunk_index,
                    source_cursor, content, content_bytes)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                payload_chunks,
            )
        if payload_manifests:
            await connection.executemany(
                """INSERT INTO projected_payload_manifest
                   (payload_ref, conversation_id, item_id, field_name, source_id, generation_id,
                    revision, present, chunk_count, content_bytes, source_cursor)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
                payload_manifests,
            )
        if rows:
            await connection.executemany(
                _UPSERT_ROW, [tuple(row[field] for field in _ROW_FIELDS) for row in rows.values()]
            )
        await connection.execute(
            "UPDATE projection_checkpoint SET through_cursor = $2 WHERE conversation_id = $1", conversation_id, through
        )
        return ApplyResult(through, duplicate_events, len(rows), len(payload_manifests))

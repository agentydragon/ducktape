"""Read-only SQL inspection of the central sampler database in blackbox tests."""

import gzip
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.proto import event_log_pb2


@dataclass(frozen=True)
class SessionSnapshot:
    cursor: int
    blocked: bool


@dataclass(frozen=True)
class IntentSnapshot:
    id: str
    request: dict[str, Any]
    result: dict[str, Any] | None
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProcessSnapshot:
    id: str
    conversation_id: str
    status: str
    buffered_output: bytes
    conversation_owner: str | None


def _loads(value: str | bytes | bytearray | memoryview | None) -> Any:
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    return json.loads(value)


def _json(value: str | bytes | bytearray | memoryview | None) -> dict[str, Any] | None:
    decoded = _loads(value)
    if decoded is None:
        return None
    assert isinstance(decoded, dict)
    return decoded


def _content_references(kind: str, payload: dict[str, Any]) -> list[str]:
    """References in sampler's typed Responses and Anthropic storage records only."""
    refs: list[str] = []

    def add(value: Any) -> None:
        if value is None:
            return
        assert isinstance(value, str), f"invalid {kind} content reference: {value!r}"
        assert value, f"empty {kind} content reference"
        refs.append(value)

    if kind == "openai_request":
        add(payload.get("instructions_ref"))
        for value in payload.get("tool_refs", []):
            add(value)
    elif kind == "anthropic_message":
        mode = payload.get("content_mode")
        if mode == "array":
            for value in payload.get("blocks", []):
                add(value)
    elif kind == "anthropic_request":
        add(payload["system_id"])
        for value in payload["tools"]:
            add(value)
    return refs


class Store:
    """Read-only queries for assertions; all state changes go through the Go service."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self._content_cache: dict[str, tuple[str, Any]] = {}
        self._context_cache: dict[str, list[str]] = {}

    async def _context_record_ids(self, heads: list[str]) -> dict[str, list[str]]:
        unique = list(dict.fromkeys(head for head in heads if head))
        missing = [head for head in unique if head not in self._context_cache]
        if missing:
            query = text(
                "WITH RECURSIVE prefix(head_id, id, api, parent_id, content_id, item_count) AS ("
                " SELECT roots.head_id, c.id, c.api, c.parent_id, c.content_id, c.item_count"
                " FROM unnest(CAST(:heads AS text[])) AS roots(head_id)"
                " JOIN sampler_context c ON c.id=roots.head_id"
                " UNION ALL"
                " SELECT prefix.head_id, p.id, p.api, p.parent_id, p.content_id, p.item_count"
                " FROM prefix JOIN sampler_context p"
                " ON p.id=prefix.parent_id AND p.item_count=prefix.item_count-1 AND p.api=prefix.api"
                ") SELECT head_id, id, api, parent_id, content_id, item_count"
                " FROM prefix ORDER BY head_id, item_count"
            )
            async with self.engine.connect() as connection:
                rows = (await connection.execute(query, {"heads": missing})).all()
            by_head: dict[str, list[tuple[str, str, str | None, str, int]]] = {head: [] for head in missing}
            for row in rows:
                by_head[row.head_id].append((row.id, row.api, row.parent_id, row.content_id, row.item_count))
            for head, chain in by_head.items():
                assert chain, f"missing sampler context {head}"
                ids: list[str] = []
                previous_id: str | None = None
                previous_count = 0
                api = chain[0][1]
                for context_id, row_api, parent_id, content_id, count in chain:
                    assert row_api == api
                    assert parent_id == previous_id
                    assert count == previous_count + 1
                    ids.append(content_id)
                    previous_id, previous_count = context_id, count
                assert previous_id == head, f"incomplete sampler context {head}"
                self._context_cache[head] = ids
        return {head: list(self._context_cache[head]) for head in unique}

    async def _prefetch_content(self, root_ids: list[str]) -> None:
        level = list(dict.fromkeys(content_id for content_id in root_ids if content_id))
        while level:
            missing = [content_id for content_id in level if content_id not in self._content_cache]
            for start in range(0, len(missing), 500):
                batch = missing[start : start + 500]
                query = text("SELECT id, kind, payload FROM sampler_content WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                )
                async with self.engine.connect() as connection:
                    rows = (await connection.execute(query, {"ids": batch})).all()
                for row in rows:
                    payload = _loads(row.payload)
                    assert payload is not None
                    self._content_cache[row.id] = (row.kind, payload)
            absent = [content_id for content_id in missing if content_id not in self._content_cache]
            assert not absent, f"missing sampler content rows: {absent[:3]}"
            next_level: list[str] = []
            for content_id in level:
                kind, payload = self._content_cache[content_id]
                if kind in {"openai_request", "anthropic_message", "anthropic_request"}:
                    assert isinstance(payload, dict), f"sampler content {content_id} is not an object"
                    next_level.extend(_content_references(kind, payload))
            level = list(dict.fromkeys(next_level))

    def _content(self, content_id: str, expected_kind: str | None = None) -> Any:
        kind, payload = self._content_cache[content_id]
        if expected_kind is not None:
            assert kind == expected_kind, f"sampler content {content_id} has kind {kind}, expected {expected_kind}"
        return payload

    def _restore_anthropic_record(self, content_id: str) -> dict[str, Any]:
        message = self._content(content_id, "anthropic_message")
        result = dict(message.get("fields", {}))
        mode = message["content_mode"]
        if mode == "array":
            result["content"] = [dict(self._content(ref)) for ref in message.get("blocks", [])]
        return result

    def _restore_record(self, api: str, content_id: str) -> dict[str, Any]:
        if api == "responses":
            return dict(self._content(content_id, "openai_item"))
        assert api == "anthropic_messages", f"unsupported sampler API {api}"
        return self._restore_anthropic_record(content_id)

    def _restore_request(self, request: dict[str, Any], record_ids: list[str]) -> dict[str, Any] | None:
        profile_id = request.get("profile_id")
        api = request.get("api")
        if not profile_id or not api:
            return None
        profile_kind = "openai_request" if api == "responses" else "anthropic_request"
        profile = self._content(profile_id, profile_kind)
        records = [self._restore_record(api, record_id) for record_id in record_ids]
        if api == "responses":
            body: dict[str, Any] = {}
            for field in (
                "model",
                "stream",
                "store",
                "include",
                "prompt_cache_key",
                "client_metadata",
                "reasoning",
                "parallel_tool_calls",
            ):
                if field in profile:
                    body[field] = profile[field]
            body["instructions"] = self._content(profile["instructions_ref"], "text")
            if profile.get("tool_refs"):
                body["tools"] = [self._content(ref, "openai_tool") for ref in profile["tool_refs"]]
            body["input"] = records
            return body

        body = dict(profile.get("fields", {}))
        body["system"] = self._content(profile["system_id"], "text")
        body["tools"] = [self._content(ref, "anthropic_tool") for ref in profile["tools"]]
        body["messages"] = records
        return body

    async def get_session(self, session_id: str) -> SessionSnapshot:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT cursor, blocked FROM sampler_session WHERE id=:session_id"), {"session_id": session_id}
                )
            ).one()
        return SessionSnapshot(cursor=row.cursor, blocked=row.blocked)

    async def transcript_items(self, session_id: str) -> list[dict[str, Any]]:
        """Append-only conversation transcript, retained across compaction."""
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT c.api, c.transcript_context_id FROM sampler_conversation c "
                        "JOIN sampler_session s ON s.conversation_id=c.id WHERE s.id=:session_id"
                    ),
                    {"session_id": session_id},
                )
            ).one()
        if row.transcript_context_id is None:
            return []
        return await self._records(row.api, row.transcript_context_id)

    async def active_context(self, session_id: str) -> list[dict[str, Any]]:
        """The active native model context, exposed as its record list for assertions."""
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT c.api, c.active_context_id, c.transcript_context_id FROM sampler_conversation c "
                        "JOIN sampler_session s ON s.conversation_id=c.id WHERE s.id=:session_id"
                    ),
                    {"session_id": session_id},
                )
            ).one()
        context_id = row.active_context_id or row.transcript_context_id
        if context_id is None:
            return []
        return await self._records(row.api, context_id)

    async def _records(self, api: str, context_id: str) -> list[dict[str, Any]]:
        ids = (await self._context_record_ids([context_id]))[context_id]
        await self._prefetch_content(ids)
        return [self._restore_record(api, content_id) for content_id in ids]

    async def released_generation(self, session_id: str) -> int | None:
        async with self.engine.connect() as connection:
            generation = await connection.scalar(
                text(
                    "SELECT c.generation FROM sampler_conversation c "
                    "JOIN sampler_session s ON s.conversation_id=c.id "
                    "WHERE s.id=:session_id AND c.owner IS NULL"
                ),
                {"session_id": session_id},
            )
        return None if generation is None else int(generation)

    async def resident_generation(self, session_id: str) -> int | None:
        async with self.engine.connect() as connection:
            generation = await connection.scalar(
                text(
                    "SELECT c.generation FROM sampler_conversation c "
                    "JOIN sampler_session s ON s.conversation_id=c.id "
                    "WHERE s.id=:session_id AND s.active_turn IS NULL "
                    "AND c.owner IS NOT NULL AND c.lease_until>clock_timestamp()"
                ),
                {"session_id": session_id},
            )
        return None if generation is None else int(generation)

    async def has_live_cell(self, session_id: str) -> bool:
        async with self.engine.connect() as connection:
            found = await connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM sampler_cell c "
                    "JOIN sampler_session s ON s.conversation_id=c.conversation_id "
                    "WHERE s.id=:session_id AND c.status='live')"
                ),
                {"session_id": session_id},
            )
        return bool(found)

    async def intents(self) -> list[IntentSnapshot]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text("SELECT id::text, request::text, result::text FROM sampler_intent ORDER BY id")
                )
            ).all()
        parsed: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
        context_heads: list[str] = []
        content_roots: list[str] = []
        for row in rows:
            request = _json(row.request)
            assert request is not None
            parsed.append((row[0], request, _json(row.result)))
            if request.get("context_id"):
                context_heads.append(request["context_id"])
            if request.get("profile_id"):
                content_roots.append(request["profile_id"])
        contexts = await self._context_record_ids(context_heads)
        for ids in contexts.values():
            content_roots.extend(ids)
        await self._prefetch_content(content_roots)
        intents = []
        for intent_id, request, result in parsed:
            record_ids = contexts.get(request.get("context_id", ""), [])
            body = self._restore_request(request, record_ids)
            intents.append(IntentSnapshot(id=intent_id, request=request, result=result, body=body))
        return intents

    async def process(self, session_id: int) -> ProcessSnapshot | None:
        """The process the model knows by unified exec `session_id` (unique in this fixture)."""
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT p.id, p.conversation_id::text, p.status, p.buffered_output, c.owner::text "
                        "FROM sampler_process p JOIN sampler_conversation c ON c.id=p.conversation_id "
                        "WHERE p.model_session_id=:session_id"
                    ),
                    {"session_id": session_id},
                )
            ).one_or_none()
        if row is None:
            return None
        return ProcessSnapshot(
            id=row.id,
            conversation_id=row.conversation_id,
            status=row.status,
            buffered_output=row.buffered_output,
            conversation_owner=row.owner,
        )

    async def queued_notifications(self, session_id: str) -> list[str]:
        async with self.engine.connect() as connection:
            texts = (
                await connection.scalars(
                    text(
                        "SELECT n.text FROM sampler_notification n "
                        "JOIN sampler_session s ON s.conversation_id=n.conversation_id "
                        "WHERE s.id=:session_id ORDER BY n.sequence"
                    ),
                    {"session_id": session_id},
                )
            ).all()
        return list(texts)

    async def events(self, session_id: str, after: int, *, limit: int = 256) -> list[event_log_pb2.EventEntry]:
        async with self.engine.connect() as connection:
            entries = (
                await connection.scalars(
                    text(
                        "SELECT entry FROM sampler_event "
                        "WHERE session_id=:session_id AND sequence>:after "
                        "ORDER BY sequence LIMIT :limit"
                    ),
                    {"session_id": session_id, "after": after, "limit": limit},
                )
            ).all()
        decoded = []
        for entry in entries:
            raw = bytes(entry)
            if raw.startswith(b"\x1f\x8b"):
                raw = gzip.decompress(raw)
            decoded.append(event_log_pb2.EventEntry.FromString(raw))
        return decoded


# The protobuf-generated stubs import google.protobuf directly.
# gazelle:include_dep @pypi//protobuf

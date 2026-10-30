"""Read-only SQL inspection of the central sampler database in blackbox tests."""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
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


@dataclass(frozen=True)
class ProcessSnapshot:
    id: str
    conversation_id: str
    status: str
    buffered_output: bytes
    conversation_owner: str | None


def _json(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    decoded: Any = json.loads(value)
    assert isinstance(decoded, dict)
    return decoded


class Store:
    """Read-only queries for assertions; all state changes go through the Go service."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def get_session(self, session_id: str) -> SessionSnapshot:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT cursor, blocked FROM sampler_session WHERE id=:session_id"), {"session_id": session_id}
                )
            ).one()
        return SessionSnapshot(cursor=row.cursor, blocked=row.blocked)

    async def archive_items(self, session_id: str) -> list[dict[str, Any]]:
        """Append-only conversation transcript, retained across compaction."""
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT item::text FROM sampler_conversation_item i "
                        "JOIN sampler_session s ON s.conversation_id=i.conversation_id "
                        "WHERE s.id=:session_id ORDER BY i.ordinal"
                    ),
                    {"session_id": session_id},
                )
            ).all()
        decoded = [json.loads(row[0]) for row in rows]
        assert all(isinstance(item, dict) for item in decoded)
        return decoded

    async def active_context(self, session_id: str) -> list[dict[str, Any]]:
        """The current model context, falling back to the full archive before compaction."""
        async with self.engine.connect() as connection:
            encoded = await connection.scalar(
                text(
                    "SELECT c.context_items::text FROM sampler_conversation c "
                    "JOIN sampler_session s ON s.conversation_id=c.id WHERE s.id=:session_id"
                ),
                {"session_id": session_id},
            )
        if encoded is None or encoded == "null":
            return await self.archive_items(session_id)
        decoded: Any = json.loads(encoded)
        assert isinstance(decoded, list)
        assert all(isinstance(item, dict) for item in decoded)
        return decoded

    async def idle_generation(self, session_id: str) -> int | None:
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
        intents = []
        for row in rows:
            request = _json(row.request)
            assert request is not None
            intents.append(IntentSnapshot(id=row[0], request=request, result=_json(row.result)))
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

    async def processes_for_session(self, session_id: str) -> list[ProcessSnapshot]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT p.id, p.conversation_id::text, p.status, p.buffered_output, c.owner::text "
                        "FROM sampler_process p "
                        "JOIN sampler_conversation c ON c.id=p.conversation_id "
                        "JOIN sampler_session s ON s.conversation_id=c.id "
                        "WHERE s.id=:session_id ORDER BY p.id"
                    ),
                    {"session_id": session_id},
                )
            ).all()
        return [
            ProcessSnapshot(
                id=row.id,
                conversation_id=row.conversation_id,
                status=row.status,
                buffered_output=row.buffered_output,
                conversation_owner=row.owner,
            )
            for row in rows
        ]

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
        return [event_log_pb2.EventEntry.FromString(entry) for entry in entries]


# The protobuf-generated stubs import google.protobuf directly.
# gazelle:include_dep @pypi//protobuf

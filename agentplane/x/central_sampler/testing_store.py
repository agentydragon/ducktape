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

    async def process(self, process_id: str) -> ProcessSnapshot | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT p.id, p.conversation_id::text, p.status, p.buffered_output, c.owner::text "
                        "FROM sampler_process p JOIN sampler_conversation c ON c.id=p.conversation_id "
                        "WHERE p.id=:process_id"
                    ),
                    {"process_id": process_id},
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

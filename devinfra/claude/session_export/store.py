"""PostgreSQL mirror of the synced sessions and their events (design: docs/sync.md)."""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from devinfra.claude.session_export.models import (
    DEFAULT_ATTESTATION_STATUS,
    SESSION_STATUS_ARCHIVED,
    Event,
    SessionSummary,
    canonical_id,
    parse_timestamp,
)

# gazelle:include_dep @pypi//asyncpg

logger = logging.getLogger(__name__)

# One statement binds at most 32767 parameters.
SESSIONS_PER_STATEMENT = 500
EVENTS_PER_STATEMENT = 500

# jsonb cannot hold U+0000. The escape is live only after an even run of backslashes: `\\u0000` is a backslash
# followed by the text "u0000".
_NUL_ESCAPE = re.compile(r"(?<!\\)((?:\\\\)*)\\u0000")
SYMBOL_FOR_NULL = "␀"


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), comment="As the API lists it now.")
    synced_last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="`last_event_at` as listed before the last completed event pass; NULL until the first one finishes.",
    )
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="The list item as sent.")


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (Index("events_by_type", "event_type", "created_at"),)

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"), primary_key=True)
    sequence_num: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event_id: Mapped[UUID] = mapped_column(comment="Not unique: the API repeats ids across sessions.")
    event_type: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), comment="Not monotonic in `sequence_num`; order by that."
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # TODO: json or jsonb? jsonb rejects NUL, so `dumps_jsonb` rewrites it (151 of 5.2M events); json keeps every
    # byte but errors at query time on those rows and has no containment or GIN. See TODO.md.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


def dumps_jsonb(document: Any) -> str:
    """JSON text for a jsonb column, each U+0000 rewritten to U+2400 and the count logged."""
    text = json.dumps(document, ensure_ascii=False)
    if "\\u0000" not in text:
        return text
    text, replaced = _NUL_ESCAPE.subn(lambda match: match[1] + SYMBOL_FOR_NULL, text)
    if replaced:
        logger.warning("replaced %d NUL character(s) with U+2400 in one JSON document", replaced)
    return text


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        make_url(database_url).set(drivername="postgresql+asyncpg"),
        json_serializer=dumps_jsonb,
        pool_size=4,
        pool_pre_ping=True,
        hide_parameters=True,  # payloads are transcripts
    )


def optional_timestamp(value: str | None) -> datetime | None:
    return None if value is None else parse_timestamp(value)


def session_values(session: SessionSummary) -> dict[str, Any]:
    return {
        "session_id": canonical_id(session.id),
        "title": session.title,
        "status": session.status,
        "created_at": parse_timestamp(session.created_at),
        "updated_at": parse_timestamp(session.updated_at),
        "last_event_at": parse_timestamp(session.last_event_at),
        "raw": session.model_dump(mode="json"),
    }


def event_values(session_id: str, event: Event) -> dict[str, Any]:
    # The store drops both fields, so anything but the default would be lost data.
    if event.device_attestation_status != DEFAULT_ATTESTATION_STATUS or event.sent_by_account_id is not None:
        raise ValueError(
            f"{session_id=} sequence_num={event.sequence_num}: unstored envelope fields carry data:"
            f" {event.device_attestation_status=} {event.sent_by_account_id=}"
        )
    return {
        "session_id": session_id,
        "sequence_num": event.seq,
        "event_id": UUID(event.event_id),
        "event_type": event.event_type,
        "source": event.source,
        "created_at": parse_timestamp(event.created_at),
        "received_at": optional_timestamp(event.received_at),
        "processing_at": optional_timestamp(event.processing_at),
        "processed_at": optional_timestamp(event.processed_at),
        "payload": event.payload,
    }


@dataclass(frozen=True)
class StoreCounts:
    sessions: int
    behind: int


class SessionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert_sessions(self, sessions: Sequence[SessionSummary]) -> None:
        """Refresh the listed metadata; `synced_last_event_at` is left alone."""
        async with self._engine.begin() as connection:
            for batch in batched((session_values(s) for s in sessions), SESSIONS_PER_STATEMENT, strict=False):
                statement = insert(SessionRow).values(batch)
                await connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[SessionRow.session_id],
                        set_={
                            column: statement.excluded[column]
                            for column in ("title", "status", "created_at", "updated_at", "last_event_at", "raw")
                        },
                    )
                )

    async def synced_last_event_at(self) -> dict[str, datetime | None]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(select(SessionRow.session_id, SessionRow.synced_last_event_at))
            return dict(rows.tuples().all())

    async def live_session_ids(self, *, active_since: datetime, limit: int) -> list[str]:
        """The sessions worth a live stream: not archived, with an event since `active_since`; newest first."""
        query = (
            select(SessionRow.session_id)
            .where(SessionRow.status != SESSION_STATUS_ARCHIVED, SessionRow.last_event_at >= active_since)
            .order_by(SessionRow.last_event_at.desc())
            .limit(limit)
        )
        async with self._engine.connect() as connection:
            return list((await connection.execute(query)).scalars())

    async def counts(self) -> StoreCounts:
        """How many sessions are stored and how many of those are behind the API."""
        behind = SessionRow.synced_last_event_at.is_distinct_from(SessionRow.last_event_at)
        async with self._engine.connect() as connection:
            sessions, lagging = (
                await connection.execute(select(func.count(), func.count().filter(behind)).select_from(SessionRow))
            ).one()
        return StoreCounts(sessions=sessions, behind=lagging)

    async def resume_after(self, session_id: str) -> int:
        """The `sequence_num` to read after: the newest stored, or just before the earliest event the worker had
        not finished when it was stored (its stamps may have moved on since)."""
        in_flight = EventRow.received_at.is_not(None) & EventRow.processed_at.is_(None)
        query = select(
            func.coalesce(func.min(EventRow.sequence_num).filter(in_flight) - 1, func.max(EventRow.sequence_num), 0)
        ).where(EventRow.session_id == session_id)
        async with self._engine.connect() as connection:
            return (await connection.execute(query)).scalar_one()

    async def sequence_num_of(self, session_id: str, event_id: UUID) -> int | None:
        """The newest stored event of the session with this `event_id`, if any."""
        query = (
            select(EventRow.sequence_num)
            .where(EventRow.session_id == session_id, EventRow.event_id == event_id)
            .order_by(EventRow.sequence_num.desc())
            .limit(1)
        )
        async with self._engine.connect() as connection:
            return (await connection.execute(query)).scalar_one_or_none()

    async def append_events(self, session_id: str, events: Sequence[Event]) -> None:
        """Insert new events; one already stored only has its worker stamps refreshed."""
        async with self._engine.begin() as connection:
            for batch in batched((event_values(session_id, e) for e in events), EVENTS_PER_STATEMENT, strict=False):
                statement = insert(EventRow).values(batch)
                stamps = (EventRow.received_at, EventRow.processing_at, EventRow.processed_at)
                await connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[EventRow.session_id, EventRow.sequence_num],
                        set_={stamp.key: statement.excluded[stamp.key] for stamp in stamps},
                        where=or_(*(stamp.is_distinct_from(statement.excluded[stamp.key]) for stamp in stamps)),
                    )
                )

    async def mark_synced(self, session_id: str, last_event_at: datetime) -> None:
        """Record that every event up to the session's `last_event_at`, as listed before the pass, is stored."""
        async with self._engine.begin() as connection:
            await connection.execute(
                update(SessionRow).where(SessionRow.session_id == session_id).values(synced_last_event_at=last_event_at)
            )

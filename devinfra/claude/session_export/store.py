"""PostgreSQL mirror of the synced sessions and their events (design: docs/sync.md)."""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    and_,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
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
CHANGE_RETENTION_REVISIONS = 10_000
SESSION_CHANGE_CHANNEL = "session_change"

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


class SessionChangeStateRow(Base):
    __tablename__ = "session_change_state"
    __table_args__ = (CheckConstraint("singleton = 1", name="session_change_state_singleton"),)

    singleton: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    oldest_retained: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SessionChangeRow(Base):
    __tablename__ = "session_changes"

    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, primary_key=True)


@dataclass(frozen=True)
class ChangedSessions:
    revision: int
    session_ids: tuple[str, ...]


@dataclass(frozen=True)
class SessionChangeBatch:
    current_revision: int
    oldest_retained: int
    changes: tuple[ChangedSessions, ...]


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
            changed_session_ids: list[str] = []
            for batch in batched((session_values(s) for s in sessions), SESSIONS_PER_STATEMENT, strict=False):
                statement = insert(SessionRow).values(batch)
                changed = await connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[SessionRow.session_id],
                        set_={
                            column: statement.excluded[column]
                            for column in ("title", "status", "created_at", "updated_at", "last_event_at", "raw")
                        },
                        where=or_(
                            SessionRow.title.is_distinct_from(statement.excluded.title),
                            SessionRow.status.is_distinct_from(statement.excluded.status),
                            SessionRow.created_at.is_distinct_from(statement.excluded.created_at),
                            SessionRow.updated_at.is_distinct_from(statement.excluded.updated_at),
                            SessionRow.last_event_at.is_distinct_from(statement.excluded.last_event_at),
                            SessionRow.raw.is_distinct_from(statement.excluded.raw),
                        ),
                    ).returning(SessionRow.session_id)
                )
                changed_session_ids.extend(changed.scalars().all())
            await self._record_changes(connection, changed_session_ids)

    async def current_change_revision(self) -> int:
        async with self._engine.connect() as connection:
            return (
                await connection.execute(
                    select(SessionChangeStateRow.revision).where(SessionChangeStateRow.singleton == 1)
                )
            ).scalar_one()

    async def changes_after(self, revision: int) -> SessionChangeBatch:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(
                        SessionChangeStateRow.revision,
                        SessionChangeStateRow.oldest_retained,
                        SessionChangeRow.revision,
                        SessionChangeRow.session_id,
                    )
                    .select_from(SessionChangeStateRow)
                    .outerjoin(SessionChangeRow, SessionChangeRow.revision > revision)
                    .where(SessionChangeStateRow.singleton == 1)
                    .order_by(SessionChangeRow.revision, SessionChangeRow.session_id)
                )
            ).all()

        grouped: dict[int, list[str]] = {}
        for _, _, changed_revision, session_id in rows:
            if changed_revision is not None and session_id is not None:
                grouped.setdefault(changed_revision, []).append(session_id)
        return SessionChangeBatch(
            current_revision=rows[0][0],
            oldest_retained=rows[0][1],
            changes=tuple(ChangedSessions(key, tuple(ids)) for key, ids in grouped.items()),
        )

    @asynccontextmanager
    async def listen_for_changes(self) -> AsyncIterator[asyncio.Event]:
        """Listen for durable DB changes; notifications only wake readers to replay the journal."""
        connection = await asyncpg.connect(
            self._engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
        )
        notified = asyncio.Event()

        async def on_notification(_connection: object, _pid: int, _channel: str, _payload: object, /) -> None:
            notified.set()

        listener_added = False
        try:
            await connection.add_listener(SESSION_CHANGE_CHANNEL, on_notification)
            listener_added = True
            yield notified
        finally:
            try:
                if listener_added:
                    await connection.remove_listener(SESSION_CHANGE_CHANNEL, on_notification)
            finally:
                await connection.close()

    async def _record_changes(self, connection: AsyncConnection, session_ids: Collection[str]) -> None:
        unique_ids = sorted(set(session_ids))
        if not unique_ids:
            return

        revision = (
            await connection.execute(
                update(SessionChangeStateRow)
                .where(SessionChangeStateRow.singleton == 1)
                .values(revision=SessionChangeStateRow.revision + 1)
                .returning(SessionChangeStateRow.revision)
            )
        ).scalar_one()
        await connection.execute(
            insert(SessionChangeRow), [{"revision": revision, "session_id": session_id} for session_id in unique_ids]
        )
        oldest_retained = max(0, revision - CHANGE_RETENTION_REVISIONS)
        if oldest_retained > 0:
            await connection.execute(delete(SessionChangeRow).where(SessionChangeRow.revision <= oldest_retained))
            await connection.execute(
                update(SessionChangeStateRow)
                .where(SessionChangeStateRow.singleton == 1)
                .values(oldest_retained=oldest_retained)
            )
        await connection.execute(select(func.pg_notify(SESSION_CHANGE_CHANNEL, str(revision))))

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

    async def counts(self, *, followed: Collection[str]) -> StoreCounts:
        """How many sessions are stored, and how many are behind the API with no live stream to keep them current."""
        behind = SessionRow.synced_last_event_at.is_distinct_from(
            SessionRow.last_event_at
        ) & SessionRow.session_id.not_in(followed)
        async with self._engine.connect() as connection:
            sessions, lagging = (
                await connection.execute(select(func.count(), func.count().filter(behind)).select_from(SessionRow))
            ).one()
        return StoreCounts(sessions=sessions, behind=lagging)

    async def session_page(
        self, *, limit: int, statuses: Collection[str] | None, before: tuple[datetime, str] | None
    ) -> tuple[list[SessionSummary], bool, tuple[datetime, str] | None]:
        """Read sessions newest-first, with a stable cursor over `(last_event_at, session_id)`."""
        query = select(SessionRow.last_event_at, SessionRow.session_id, SessionRow.raw)
        if statuses is not None:
            query = query.where(SessionRow.status.in_(statuses))
        if before is not None:
            timestamp, session_id = before
            query = query.where(
                or_(
                    SessionRow.last_event_at < timestamp,
                    and_(SessionRow.last_event_at == timestamp, SessionRow.session_id < session_id),
                )
            )
        query = query.order_by(SessionRow.last_event_at.desc(), SessionRow.session_id.desc()).limit(limit + 1)
        async with self._engine.connect() as connection:
            rows = list((await connection.execute(query)).mappings())
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (rows[-1]["last_event_at"], rows[-1]["session_id"]) if has_more and rows else None
        return [SessionSummary.model_validate(row["raw"]) for row in rows], has_more, next_cursor

    async def session(self, session_id: str) -> SessionSummary | None:
        async with self._engine.connect() as connection:
            raw = await connection.scalar(select(SessionRow.raw).where(SessionRow.session_id == session_id))
        return SessionSummary.model_validate(raw) if raw is not None else None

    async def event_page(
        self, session_id: str, *, limit: int, sort_order: str, after: int | None, before: int | None
    ) -> tuple[list[Event], bool]:
        """Read events in sequence order; `after` and `before` make cursors stable across concurrent writes."""
        query = select(EventRow.__table__).where(EventRow.session_id == session_id)
        if sort_order == "asc":
            if after is not None:
                query = query.where(EventRow.sequence_num > after)
            query = query.order_by(EventRow.sequence_num.asc())
        else:
            if before is not None:
                query = query.where(EventRow.sequence_num < before)
            query = query.order_by(EventRow.sequence_num.desc())
        async with self._engine.connect() as connection:
            rows = list((await connection.execute(query.limit(limit + 1))).mappings())
        has_more = len(rows) > limit
        rows = rows[:limit]
        return [
            Event.model_validate(
                {
                    "event_id": str(row["event_id"]),
                    "sequence_num": str(row["sequence_num"]),
                    "event_type": row["event_type"],
                    "source": row["source"],
                    "created_at": row["created_at"].isoformat(),
                    "received_at": row["received_at"].isoformat() if row["received_at"] else None,
                    "processing_at": row["processing_at"].isoformat() if row["processing_at"] else None,
                    "processed_at": row["processed_at"].isoformat() if row["processed_at"] else None,
                    "device_attestation_status": DEFAULT_ATTESTATION_STATUS,
                    "sent_by_account_id": None,
                    "payload": row["payload"],
                }
            )
            for row in rows
        ], has_more

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
            changed = False
            for batch in batched((event_values(session_id, e) for e in events), EVENTS_PER_STATEMENT, strict=False):
                statement = insert(EventRow).values(batch)
                stamps = (EventRow.received_at, EventRow.processing_at, EventRow.processed_at)
                rows = await connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[EventRow.session_id, EventRow.sequence_num],
                        set_={stamp.key: statement.excluded[stamp.key] for stamp in stamps},
                        where=or_(*(stamp.is_distinct_from(statement.excluded[stamp.key]) for stamp in stamps)),
                    ).returning(EventRow.sequence_num)
                )
                changed = rows.scalars().first() is not None or changed
            if changed:
                await self._record_changes(connection, [session_id])

    async def mark_synced(self, session_id: str, last_event_at: datetime) -> None:
        """Record that every event up to the session's `last_event_at`, as listed before the pass, is stored."""
        async with self._engine.begin() as connection:
            await connection.execute(
                update(SessionRow).where(SessionRow.session_id == session_id).values(synced_last_event_at=last_event_at)
            )

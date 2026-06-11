from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
from asyncpg import Connection
from asyncpg.pool import PoolConnectionProxy
from openai.types.responses import Response as OpenAIResponse, ResponseStreamEvent, ResponseUsage
from pydantic import TypeAdapter
from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from rspcache.events import (
    APIKeyCreatedEvent,
    APIKeyRevokedEvent,
    EventPayload,
    FrameAppendedEvent,
    ResponseStatusEvent,
    parse_event,
)
from rspcache.models import ErrorPayload, FinalResponseSnapshot, ResponseStatus, response_from_event

__all__ = [
    "APIKeyRecord",
    "ClientAPIKey",
    "Response",
    "ResponseDetail",
    "ResponseFrame",
    "ResponseSnapshot",
    "ResponsesDB",
]

CHANNEL_NAME = "rspcache_events"

# Type adapter for OpenAI SDK type alias (not a Pydantic model)
_response_stream_event_adapter: TypeAdapter[ResponseStreamEvent] = TypeAdapter(ResponseStreamEvent)


class Base(DeclarativeBase):
    pass


class ClientAPIKey(Base):
    __tablename__ = "client_api_keys"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_salt: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    upstream_alias: Mapped[str] = mapped_column(
        "openai_key_alias",
        String,
        nullable=False,
        default="default",
        doc="Logical name for selecting an upstream OpenAI API key (see _load_openai_keys).",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    responses: Mapped[list[Response]] = relationship(back_populates="api_key", cascade="all, delete-orphan")


class ResponseFrame(Base):
    __tablename__ = "response_frames"
    __table_args__ = (
        Index("idx_response_frames_cache_key_sequence", "cache_key", "sequence_number"),
        Index("idx_response_frames_response_id_sequence", "response_id", "sequence_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(
        String, ForeignKey("responses.cache_key", ondelete="CASCADE"), nullable=False
    )
    response_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_type: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    frame: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    response: Mapped[Response] = relationship(back_populates="frames")

    @property
    def typed_frame(self) -> ResponseStreamEvent:
        """Return the frame as a typed ResponseStreamEvent."""
        return _response_stream_event_adapter.validate_python(self.frame)


class Response(Base):
    __tablename__ = "responses"
    __table_args__ = (
        Index("idx_responses_created_at", "created_at"),
        Index("idx_responses_model", "model"),
        Index("idx_responses_response_id", "response_id"),
    )

    cache_key: Mapped[str] = mapped_column(String, primary_key=True)
    response_id: Mapped[str | None] = mapped_column(String, unique=True)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("client_api_keys.id"))
    model: Mapped[str] = mapped_column(String, nullable=False)
    request_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[ResponseStatus] = mapped_column(
        SQLEnum(ResponseStatus, name="rspcache_response_status", native_enum=False),
        nullable=False,
        default=ResponseStatus.QUEUED,
    )
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    api_key: Mapped[ClientAPIKey | None] = relationship(back_populates="responses")
    frames: Mapped[list[ResponseFrame]] = relationship(back_populates="response", cascade="all, delete-orphan")
    snapshot: Mapped[ResponseSnapshot | None] = relationship(
        back_populates="response", cascade="all, delete-orphan", uselist=False
    )


class ResponseSnapshot(Base):
    __tablename__ = "response_snapshots"

    cache_key: Mapped[str] = mapped_column(
        String, ForeignKey("responses.cache_key", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[ResponseStatus] = mapped_column(
        SQLEnum(ResponseStatus, name="rspcache_snapshot_status", native_enum=False), nullable=False
    )
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    response_rel: Mapped[Response] = relationship(back_populates="snapshot")

    def to_model(self) -> FinalResponseSnapshot:
        return FinalResponseSnapshot.model_validate(
            {"status": self.status, "response": self.response, "error": self.error, "token_usage": self.token_usage}
        )


@dataclass(slots=True)
class APIKeyRecord:
    id: uuid.UUID
    name: str
    token_prefix: str
    upstream_alias: str
    created_at: datetime
    revoked_at: datetime | None


@dataclass(slots=True)
class ResponseDetail:
    record: Response
    snapshot: FinalResponseSnapshot | None


class ResponsesDB:
    def __init__(self, db_url: str | None = None):
        self._db_url = db_url or os.environ.get("ADGN_RESP_DB_URL")
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._listener_conn: asyncpg.Connection | None = None
        self._listeners: set[asyncio.Queue[EventPayload]] = set()
        self._listeners_lock = asyncio.Lock()

    async def init(self) -> None:
        if self._engine is not None:
            return
        self._engine = create_async_engine(self._require_db_url(), future=True)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as conn:
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
            await conn.execute(text("ALTER TABLE IF EXISTS client_api_keys DROP COLUMN IF EXISTS notes"))
            await conn.run_sync(Base.metadata.create_all)

    def _asyncpg_dsn(self) -> str:
        db_url = self._require_db_url()
        prefix = "postgresql+asyncpg://"
        if not db_url.startswith(prefix):
            raise RuntimeError(
                "ADGN_RESP_DB_URL must use the postgresql+asyncpg:// scheme "
                "(e.g. postgresql+asyncpg://user:pass@host:5432/dbname)"
            )
        return "postgresql://" + db_url[len(prefix) :]

    def _require_db_url(self) -> str:
        if not self._db_url:
            raise RuntimeError("ADGN_RESP_DB_URL must be set to a Postgres connection string")
        return self._db_url

    async def close(self) -> None:
        if self._listener_conn is not None:
            await self._listener_conn.remove_listener(CHANNEL_NAME, self._notify_queues)
            await self._listener_conn.close()
            self._listener_conn = None
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    # ------------------------------------------------------------------
    # API key helpers
    # ------------------------------------------------------------------

    async def create_api_key(self, name: str, alias: str = "default") -> tuple[str, APIKeyRecord]:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        token_core = secrets.token_hex(24)
        token = f"sk-rsp_{token_core}"
        prefix = token_core[:8]
        salt = os.urandom(16)
        digest = hashlib.sha256(salt + token.encode("utf-8")).digest()
        async with self._session_factory() as session:
            record = ClientAPIKey(
                name=name, token_prefix=prefix, token_hash=digest, token_salt=salt, upstream_alias=alias
            )
            session.add(record)
            await self._emit_event(session, APIKeyCreatedEvent(id=str(record.id), name=name, upstream_alias=alias))
            await session.commit()
            await session.refresh(record)
        return token, self._to_api_key_record(record)

    async def list_api_keys(self) -> Sequence[ClientAPIKey]:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            stmt = select(ClientAPIKey).order_by(ClientAPIKey.created_at.desc())
            result = await session.execute(stmt)
            return list(result.scalars())

    async def revoke_api_key(self, key_id: uuid.UUID) -> bool:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            result = await session.execute(
                update(ClientAPIKey)
                .where(ClientAPIKey.id == key_id, ClientAPIKey.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
                .returning(ClientAPIKey.id)
            )
            revoked_id = result.scalar_one_or_none()
            if revoked_id is not None:
                await self._emit_event(session, APIKeyRevokedEvent(id=str(key_id)))
            await session.commit()
            return revoked_id is not None

    async def verify_api_key(self, token: str) -> APIKeyRecord | None:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        if not token.startswith("sk-rsp_"):
            return None
        core = token[len("sk-rsp_") :]
        if len(core) < 8:
            return None
        prefix = core[:8]
        async with self._session_factory() as session:
            result = await session.execute(
                select(ClientAPIKey).where(ClientAPIKey.token_prefix == prefix, ClientAPIKey.revoked_at.is_(None))
            )
            record = result.scalar_one_or_none()
        if record is None:
            return None
        digest = hashlib.sha256(record.token_salt + token.encode("utf-8")).digest()
        if not secrets.compare_digest(record.token_hash, digest):
            return None
        return self._to_api_key_record(record)

    # ------------------------------------------------------------------
    # Response lifecycle
    # ------------------------------------------------------------------

    async def claim_key(self, key: str, model: str, request_body: dict[str, Any], api_key: APIKeyRecord | None) -> bool:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        now = datetime.now(UTC)
        stmt = (
            pg_insert(Response)
            .values(
                key=key,
                model=model,
                request_body=request_body,
                status=ResponseStatus.QUEUED,
                created_at=now,
                updated_at=now,
                api_key_id=api_key.id if api_key else None,
            )
            .on_conflict_do_nothing(index_elements=[Response.cache_key])
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt.returning(Response.cache_key))
            inserted_key = result.scalar_one_or_none()
            if inserted_key:
                await self._emit_event(
                    session, ResponseStatusEvent(cache_key=key, response_id=None, status=ResponseStatus.QUEUED)
                )
            await session.commit()
            return inserted_key is not None

    async def mark_in_progress(self, key: str, response_id: str | None) -> None:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            update_result = await session.execute(
                update(Response)
                .where(Response.cache_key == key)
                .values(status=ResponseStatus.IN_PROGRESS, response_id=response_id, updated_at=datetime.now(UTC))
                .returning(Response.cache_key)
            )
            updated_key = update_result.scalar_one_or_none()
            if updated_key:
                await self._emit_event(
                    session,
                    ResponseStatusEvent(cache_key=key, response_id=response_id, status=ResponseStatus.IN_PROGRESS),
                )
            await session.commit()

    async def append_frame(self, key: str, frame_obj: ResponseStreamEvent, *, response_id: str | None = None) -> int:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        response = response_from_event(frame_obj)
        derived_response_id = response.id if response is not None else None
        if response_id is None and derived_response_id is not None:
            response_id = derived_response_id
        frame_type = frame_obj.type
        sequence_number = frame_obj.sequence_number
        async with self._session_factory() as session:
            await self._insert_frame(
                session,
                key=key,
                frame_obj=frame_obj,
                response_id=response_id,
                frame_type=frame_type,
                sequence_number=sequence_number,
            )
            await session.execute(
                update(Response)
                .where(Response.cache_key == key)
                .values(response_id=response_id, updated_at=datetime.now(UTC))
            )
            await self._emit_event(
                session,
                FrameAppendedEvent(
                    cache_key=key, response_id=response_id, sequence_number=sequence_number, frame_type=frame_type
                ),
            )
            await session.commit()
            return sequence_number

    async def finalize_response(
        self,
        key: str,
        *,
        response_id: str | None,
        response_obj: OpenAIResponse | None,
        latency_ms: int | None,
        token_usage: ResponseUsage | None,
    ) -> None:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            await session.execute(
                update(Response)
                .where(Response.cache_key == key)
                .values(
                    status=ResponseStatus.COMPLETE,
                    response_id=response_id,
                    latency_ms=latency_ms,
                    updated_at=datetime.now(UTC),
                )
            )
            snapshot = FinalResponseSnapshot(
                status=ResponseStatus.COMPLETE, response=response_obj, error=None, token_usage=token_usage
            )
            await self._upsert_snapshot(session, key=key, snapshot=snapshot)
            await self._emit_event(
                session, ResponseStatusEvent(cache_key=key, response_id=response_id, status=ResponseStatus.COMPLETE)
            )
            await session.commit()

    async def record_error(
        self, key: str, *, error_reason: str | None, response_id: str | None, error: ErrorPayload | None
    ) -> None:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            await session.execute(
                update(Response)
                .where(Response.cache_key == key)
                .values(
                    status=ResponseStatus.ERROR,
                    error=error_reason,
                    response_id=response_id,
                    updated_at=datetime.now(UTC),
                )
            )
            snapshot = FinalResponseSnapshot(status=ResponseStatus.ERROR, response=None, error=error, token_usage=None)
            await self._upsert_snapshot(session, key=key, snapshot=snapshot)
            await self._emit_event(
                session,
                ResponseStatusEvent(
                    cache_key=key, response_id=response_id, status=ResponseStatus.ERROR, error=error_reason
                ),
            )
            await session.commit()

    async def list_responses(self, *, limit: int, offset: int = 0) -> tuple[list[Response], int]:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            stmt = (
                select(Response)
                .options(selectinload(Response.api_key), selectinload(Response.snapshot))
                .order_by(Response.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            result = await session.execute(stmt)
            items = list(result.scalars().all())
            total_value = await session.scalar(select(func.count()).select_from(Response))
        return items, int(total_value or 0)

    async def get_response(self, identifier: str) -> Response | None:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            column = Response.response_id if identifier.startswith("resp_") else Response.cache_key
            result = await session.execute(
                select(Response)
                .options(selectinload(Response.api_key), selectinload(Response.snapshot))
                .where(column == identifier)
            )
            return result.scalar_one_or_none()

    async def get_frames(
        self, identifier: str, *, limit: int | None = None, after_sequence_number: int | None = None
    ) -> list[ResponseFrame]:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            key_stmt = select(Response.cache_key).where(
                Response.response_id == identifier
                if identifier.startswith("resp_")
                else Response.cache_key == identifier
            )
            key_result = await session.execute(key_stmt)
            key_value = key_result.scalar_one_or_none()
            if key_value is None:
                return []
            stmt = (
                select(ResponseFrame)
                .where(ResponseFrame.cache_key == key_value)
                .order_by(ResponseFrame.sequence_number.asc())
            )
            if after_sequence_number is not None:
                stmt = stmt.where(ResponseFrame.sequence_number > after_sequence_number)
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars())

    # ------------------------------------------------------------------
    # Event streaming
    # ------------------------------------------------------------------

    async def _insert_frame(
        self,
        session: AsyncSession,
        *,
        key: str,
        frame_obj: ResponseStreamEvent,
        response_id: str | None,
        frame_type: str,
        sequence_number: int,
    ) -> None:
        serialized = frame_obj.model_dump(mode="json")
        frame = ResponseFrame(
            cache_key=key,
            response_id=response_id,
            sequence_number=sequence_number,
            frame_type=frame_type,
            frame=serialized,
        )
        session.add(frame)
        await session.flush()

    async def get_cached_response_payload(self, key: str) -> OpenAIResponse | None:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            snapshot = await session.get(ResponseSnapshot, key)
            if snapshot is None:
                return None
            snapshot_model = snapshot.to_model()
            if snapshot_model.status != ResponseStatus.COMPLETE or snapshot_model.response is None:
                return None
            return snapshot_model.response

    async def get_response_detail(self, identifier: str) -> ResponseDetail | None:
        if self._session_factory is None:
            raise RuntimeError("Database not initialized")
        async with self._session_factory() as session:
            if identifier.startswith("resp_"):
                result = await session.execute(
                    select(Response)
                    .options(selectinload(Response.api_key), selectinload(Response.snapshot))
                    .where(Response.response_id == identifier)
                )
            else:
                result = await session.execute(
                    select(Response)
                    .options(selectinload(Response.api_key), selectinload(Response.snapshot))
                    .where(Response.cache_key == identifier)
                )
            record = result.scalar_one_or_none()
            if record is None:
                return None
            snapshot_model = record.snapshot.to_model() if record.snapshot else None
            return ResponseDetail(record=record, snapshot=snapshot_model)

    async def _emit_event(self, session: AsyncSession, payload: EventPayload) -> None:
        await session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": CHANNEL_NAME, "payload": payload.model_dump_json()},
        )

    async def _upsert_snapshot(self, session: AsyncSession, *, key: str, snapshot: FinalResponseSnapshot) -> None:
        # TODO: Decide how to split what's stored in JSONB columns vs separate columns
        # Current: status as separate column, response/error/token_usage as JSONB
        # Consider: Could status also go in JSONB? Or extract more fields?
        existing = await session.get(ResponseSnapshot, key)
        payload = snapshot.model_dump(mode="json")
        status_enum = snapshot.status
        if existing is None:
            new_row = ResponseSnapshot(
                key=key,
                status=status_enum,
                response=payload["response"],
                error=payload["error"],
                token_usage=payload["token_usage"],
            )
            session.add(new_row)
        else:
            existing.status = status_enum
            existing.response = payload["response"]
            existing.error = payload["error"]
            existing.token_usage = payload["token_usage"]

    async def subscribe_events(self) -> AsyncIterator[EventPayload]:
        queue: asyncio.Queue[EventPayload] = asyncio.Queue()
        async with self._listeners_lock:
            self._listeners.add(queue)
        try:
            while True:
                payload = await queue.get()
                yield payload
        finally:
            async with self._listeners_lock:
                self._listeners.discard(queue)

    async def ensure_event_listener(self) -> None:
        if self._engine is None:
            raise RuntimeError("Database not initialized")
        if self._listener_conn is not None:
            return
        self._listener_conn = await asyncpg.connect(self._asyncpg_dsn())
        await self._listener_conn.add_listener(CHANNEL_NAME, self._notify_queues)

    async def stop_event_listener(self) -> None:
        if self._listener_conn is None:
            return
        await self._listener_conn.remove_listener(CHANNEL_NAME, self._notify_queues)
        await self._listener_conn.close()
        self._listener_conn = None

    # ------------------------------------------------------------------

    def _notify_queues(
        self, _conn: Connection | PoolConnectionProxy, _pid: int, _channel: str, payload: object
    ) -> None:
        event = parse_event(str(payload))
        task = asyncio.create_task(self._fanout(event))
        task.add_done_callback(lambda t: t.exception() if t.done() and not t.cancelled() else None)

    async def _fanout(self, event: EventPayload) -> None:
        async with self._listeners_lock:
            listeners = list(self._listeners)
        for queue in listeners:
            await queue.put(event)

    def _to_api_key_record(self, obj: ClientAPIKey) -> APIKeyRecord:
        return APIKeyRecord(
            id=obj.id,
            name=obj.name,
            token_prefix=obj.token_prefix,
            upstream_alias=obj.upstream_alias,
            created_at=obj.created_at,
            revoked_at=obj.revoked_at,
        )

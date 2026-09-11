"""Single-collection snapshot membership and content-addressed embedding storage."""

import hashlib
import json
import logging
import math
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    Text,
    delete,
    exists,
    func,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, defer, mapped_column
from sqlalchemy.sql import Select

from haku.recall_index.chunking import ChunkBudget, chunk_text, git_chunker_key
from haku.recall_index.content import content_sha
from haku.recall_index.embedder import Embedder
from haku.recall_index.vector_type import HalfVector

SCHEMA = "agentplane_index"
LOCK_ID = 785409112349
logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    artifact_digest: Mapped[str] = mapped_column(Text)
    revision: Mapped[str] = mapped_column(Text)
    repository_url: Mapped[str] = mapped_column(Text)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    unreachable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class State(Base):
    __tablename__ = "state"
    singleton: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(Text)
    chunker_key: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int | None] = mapped_column(Integer)
    desired: Mapped[str | None] = mapped_column(ForeignKey(Snapshot.id))
    completed: Mapped[str | None] = mapped_column(ForeignKey(Snapshot.id))


class Blob(Base):
    __tablename__ = "blobs"
    sha: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    unreachable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SnapshotFile(Base):
    __tablename__ = "snapshot_files"
    snapshot: Mapped[str] = mapped_column(ForeignKey(Snapshot.id), primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    blob: Mapped[str] = mapped_column(ForeignKey(Blob.sha))


class ServedFile(Base):
    __tablename__ = "served_files"
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    snapshot: Mapped[str] = mapped_column(ForeignKey(Snapshot.id))
    blob: Mapped[str] = mapped_column(ForeignKey(Blob.sha))


class Layout(Base):
    __tablename__ = "layouts"
    blob: Mapped[str] = mapped_column(ForeignKey(Blob.sha), primary_key=True)
    chunker_key: Mapped[str] = mapped_column(Text, primary_key=True)


class Content(Base):
    __tablename__ = "contents"
    sha: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    unreachable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chunk(Base):
    __tablename__ = "chunks"
    blob: Mapped[str] = mapped_column(ForeignKey(Blob.sha), primary_key=True)
    chunker_key: Mapped[str] = mapped_column(Text, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    byte_start: Mapped[int] = mapped_column(Integer)
    byte_end: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(ForeignKey(Content.sha))


class Embedding(Base):
    __tablename__ = "embeddings"
    content: Mapped[str] = mapped_column(ForeignKey(Content.sha), primary_key=True)
    model_key: Mapped[str] = mapped_column(Text, primary_key=True)
    vector: Mapped[list[float]] = mapped_column(HalfVector)


@dataclass(frozen=True)
class Status:
    desired_revision: str | None
    desired_repository_url: str | None
    completed_revision: str | None
    desired_digest: str | None
    pending_files: int
    served_files: int
    updating: bool


@dataclass(frozen=True)
class Hit:
    path: str
    revision: str
    repository_url: str
    artifact_digest: str
    byte_start: int
    byte_end: int
    text: str
    score: float


class Store:
    def __init__(self, engine: AsyncEngine, *, budget: ChunkBudget, model_key: str):
        self.engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.budget = budget
        self.chunker_key = git_chunker_key(budget)
        self.model_key = model_key

    @asynccontextmanager
    async def _mutation(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions.begin() as session:
            await session.execute(select(func.pg_advisory_xact_lock(LOCK_ID)))
            yield session

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(select(func.pg_advisory_xact_lock(LOCK_ID)))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
            await connection.run_sync(Base.metadata.create_all)
        async with self._mutation() as session:
            state = await session.get(State, 1)
            if state is None:
                session.add(State(singleton=1, model_key=self.model_key, chunker_key=self.chunker_key))
            elif state.model_key != self.model_key or state.chunker_key != self.chunker_key:
                raise ValueError(
                    "Index configuration changed; use a new database for a different embedding/chunking regime"
                )

    async def ingest(self, *, digest: str, revision: str, repository_url: str, files: Mapping[str, bytes]) -> None:
        snapshot_key = hashlib.sha256(json.dumps([digest, revision, repository_url]).encode()).hexdigest()
        async with self._mutation() as session:
            state = await self._state(session)
            if state.desired == snapshot_key:
                return
            snapshot = await session.get(Snapshot, snapshot_key)
            if snapshot is None:
                session.add(
                    Snapshot(
                        id=snapshot_key,
                        artifact_digest=digest,
                        revision=revision,
                        repository_url=repository_url,
                        accepted_at=datetime.now(UTC),
                    )
                )
                await session.flush()
                for path, data in files.items():
                    sha = hashlib.sha256(data).hexdigest()
                    if await session.get(Blob, sha) is None:
                        session.add(Blob(sha=sha, data=data))
                        await session.flush()
                    session.add(SnapshotFile(snapshot=snapshot_key, path=path, blob=sha))
                await session.flush()
            elif snapshot.revision != revision or snapshot.repository_url != repository_url:
                raise ValueError("Artifact digest reused with different provenance")
            else:
                snapshot.unreachable_since = None
            state.desired = snapshot_key
            desired = {
                entry.path: entry.blob
                for entry in await session.scalars(select(SnapshotFile).where(SnapshotFile.snapshot == snapshot_key))
            }
            desired_blobs = select(SnapshotFile.blob).where(SnapshotFile.snapshot == snapshot_key)
            await session.execute(update(Blob).where(Blob.sha.in_(desired_blobs)).values(unreachable_since=None))
            await session.execute(
                update(Content)
                .where(Content.sha.in_(select(Chunk.content).where(Chunk.blob.in_(desired_blobs))))
                .values(unreachable_since=None)
            )
            for served in await session.scalars(select(ServedFile)):
                if served.path not in desired:
                    await session.delete(served)
                elif served.blob == desired[served.path]:
                    served.snapshot = snapshot_key
            await session.flush()
            await self._complete(session, state)

    async def _state(self, session: AsyncSession) -> State:
        state = await session.get(State, 1)
        if state is None:
            raise RuntimeError("Store.initialize() must run first")
        return state

    @staticmethod
    def _pending(digest: str) -> Select[tuple[SnapshotFile]]:
        return select(SnapshotFile).where(
            SnapshotFile.snapshot == digest,
            ~exists().where(ServedFile.path == SnapshotFile.path, ServedFile.blob == SnapshotFile.blob),
        )

    async def _complete(self, session: AsyncSession, state: State) -> None:
        if state.desired is not None and await session.scalar(self._pending(state.desired).limit(1)) is None:
            state.completed = state.desired

    async def advance(self, embedder: Embedder, *, batch_size: int = 32) -> bool:
        """Embed one batch and publish its file only when all chunks are searchable.

        The transaction lock includes one embedding call, so GC and newer manifests cannot race
        publication. Search uses MVCC and continues reading the committed file versions.
        """
        if embedder.model_key != self.model_key:
            raise ValueError("Embedder model does not match the index")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        async with self._mutation() as session:
            state = await self._state(session)
            if state.desired is None:
                return False
            entry = await session.scalar(self._pending(state.desired).order_by(SnapshotFile.path).limit(1))
            if entry is None:
                await self._complete(session, state)
                return False
            if await session.get(Layout, (entry.blob, self.chunker_key)) is None:
                blob = await session.get(Blob, entry.blob)
                assert blob is not None
                try:
                    decoded = blob.data.decode("utf-8")
                except UnicodeDecodeError:
                    logger.info("Excluding non-UTF-8 file from embeddings: %s", entry.path)
                    decoded = ""
                if "\x00" in decoded:
                    logger.info("Excluding NUL-containing file from embeddings: %s", entry.path)
                    decoded = ""
                for ordinal, span in enumerate(chunk_text(decoded, self.budget)):
                    sha = content_sha(span.text)
                    content = await session.get(Content, sha)
                    if content is None:
                        session.add(Content(sha=sha, content=span.text))
                        await session.flush()
                    else:
                        content.unreachable_since = None
                    session.add(
                        Chunk(
                            blob=entry.blob,
                            chunker_key=self.chunker_key,
                            ordinal=ordinal,
                            byte_start=span.byte_start,
                            byte_end=span.byte_end,
                            content=sha,
                        )
                    )
                session.add(Layout(blob=entry.blob, chunker_key=self.chunker_key))
                await session.flush()
            missing = (
                select(Content)
                .join(Chunk, Chunk.content == Content.sha)
                .where(
                    Chunk.blob == entry.blob,
                    Chunk.chunker_key == self.chunker_key,
                    ~exists().where(Embedding.content == Content.sha, Embedding.model_key == self.model_key),
                )
                .distinct()
                .limit(batch_size)
            )
            if contents := list(await session.scalars(missing)):
                vectors = await embedder.embed_documents([content.content for content in contents])
                if len(vectors) != len(contents):
                    raise ValueError("Embedding backend returned the wrong number of vectors")
                for content, vector in zip(contents, vectors, strict=True):
                    if not vector or not all(math.isfinite(value) for value in vector) or not any(vector):
                        raise ValueError("Embedding backend returned an empty, zero, or nonfinite vector")
                    if state.dimensions is None:
                        state.dimensions = len(vector)
                    elif state.dimensions != len(vector):
                        raise ValueError("Embedding dimensions changed within the same model")
                    session.add(Embedding(content=content.sha, model_key=self.model_key, vector=vector))
                await session.flush()
            if await session.scalar(missing.limit(1)) is not None:
                return True
            served = await session.get(ServedFile, entry.path)
            if served is None:
                session.add(ServedFile(path=entry.path, snapshot=entry.snapshot, blob=entry.blob))
            else:
                served.snapshot, served.blob = entry.snapshot, entry.blob
            await session.flush()
            await self._complete(session, state)
            return True

    async def status(self) -> Status:
        async with self.sessions.begin() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            return await self._status(session)

    async def _status(self, session: AsyncSession) -> Status:
        state = await self._state(session)
        desired = await session.get(Snapshot, state.desired) if state.desired is not None else None
        completed = await session.get(Snapshot, state.completed) if state.completed is not None else None
        pending = (
            await session.scalar(select(func.count()).select_from(self._pending(state.desired).subquery()))
            if state.desired is not None
            else 0
        )
        return Status(
            desired_revision=desired.revision if desired else None,
            desired_repository_url=desired.repository_url if desired else None,
            completed_revision=completed.revision if completed else None,
            desired_digest=desired.artifact_digest if desired else None,
            pending_files=pending or 0,
            served_files=await session.scalar(select(func.count()).select_from(ServedFile)) or 0,
            updating=bool(pending),
        )

    async def search(self, vector: Sequence[float], *, limit: int = 10) -> tuple[list[Hit], Status]:
        if limit < 1 or not vector or not all(math.isfinite(value) for value in vector) or not any(vector):
            raise ValueError("Search requires a positive limit and a finite nonzero vector")
        distance = Embedding.vector.op("<=>", return_type=Float)(list(vector))
        query = (
            select(
                ServedFile.path,
                Snapshot.revision,
                Snapshot.repository_url,
                Snapshot.artifact_digest,
                Chunk.byte_start,
                Chunk.byte_end,
                Content.content,
                (1 - distance).label("score"),
            )
            .select_from(ServedFile)
            .join(Snapshot, Snapshot.id == ServedFile.snapshot)
            .join(Chunk, Chunk.blob == ServedFile.blob)
            .join(Content, Content.sha == Chunk.content)
            .join(Embedding, Embedding.content == Content.sha)
            .where(Chunk.chunker_key == self.chunker_key, Embedding.model_key == self.model_key)
            .order_by(distance, ServedFile.path, Chunk.ordinal)
            .limit(limit)
        )
        async with self.sessions.begin() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            state = await self._state(session)
            if state.dimensions is not None and state.dimensions != len(vector):
                raise ValueError("Query embedding dimensions do not match indexed embeddings")
            hits = [
                Hit(
                    path=row.path,
                    revision=row.revision,
                    repository_url=row.repository_url,
                    artifact_digest=row.artifact_digest,
                    byte_start=row.byte_start,
                    byte_end=row.byte_end,
                    text=row.content,
                    score=row.score,
                )
                for row in await session.execute(query)
            ]
            return hits, await self._status(session)

    async def gc(self, *, grace: timedelta, batch_size: int = 1000) -> int:
        """Mark unreachable objects and delete bounded batches after a grace period."""
        if grace < timedelta(0) or batch_size < 1:
            raise ValueError("GC needs a nonnegative grace and a positive batch size")
        now = datetime.now(UTC)
        removed = 0
        async with self._mutation() as session:
            state = await self._state(session)
            roots = set(await session.scalars(select(ServedFile.snapshot)))
            roots.update(value for value in (state.desired, state.completed) if value is not None)
            for snapshot in await session.scalars(select(Snapshot)):
                if snapshot.id in roots:
                    snapshot.unreachable_since = None
                elif snapshot.unreachable_since is None:
                    snapshot.unreachable_since = now
                elif snapshot.unreachable_since <= now - grace and removed < batch_size:
                    await session.execute(delete(SnapshotFile).where(SnapshotFile.snapshot == snapshot.id))
                    await session.delete(snapshot)
                    removed += 1
            await session.flush()
            blob_roots = set(await session.scalars(select(SnapshotFile.blob)))
            blob_roots.update(await session.scalars(select(ServedFile.blob)))
            for blob in await session.scalars(select(Blob).options(defer(Blob.data))):
                if blob.sha in blob_roots:
                    blob.unreachable_since = None
                elif blob.unreachable_since is None:
                    blob.unreachable_since = now
                elif blob.unreachable_since <= now - grace and removed < batch_size:
                    await session.execute(delete(Chunk).where(Chunk.blob == blob.sha))
                    await session.execute(delete(Layout).where(Layout.blob == blob.sha))
                    await session.delete(blob)
                    removed += 1
            await session.flush()
            content_roots = set(await session.scalars(select(Chunk.content)))
            for content in await session.scalars(select(Content).options(defer(Content.content))):
                if content.sha in content_roots:
                    content.unreachable_since = None
                elif content.unreachable_since is None:
                    content.unreachable_since = now
                elif content.unreachable_since <= now - grace and removed < batch_size:
                    await session.execute(delete(Embedding).where(Embedding.content == content.sha))
                    await session.delete(content)
                    removed += 1
        return removed

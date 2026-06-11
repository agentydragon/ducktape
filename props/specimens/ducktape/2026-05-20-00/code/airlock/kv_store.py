"""PostgreSQL-backed AsyncKeyValue store for airlock OIDC proxy client storage.

Implements the key_value.aio.protocols.AsyncKeyValue protocol, backed by a
single ``kv_store`` table with (collection, key, value, expires_at) columns.
Used by OIDCProxy's ``client_storage`` parameter to persist DCR client
registrations and token data across pod restarts.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, SupportsFloat, cast

from sqlalchemy import DateTime, String, delete, select
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)

_DEFAULT_COLLECTION = "_default"


class _KVBase(DeclarativeBase):
    pass


class _KVRow(_KVBase):
    __tablename__ = "kv_store"

    collection: Mapped[str] = mapped_column(String, primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PostgresKeyValueStore:
    """AsyncKeyValue store backed by the airlock PostgreSQL database.

    Each entry is a row in ``kv_store(collection, key, value, expires_at)``.
    Expired rows are filtered out on reads; a background cull is not needed at
    airlock's traffic volume (entries are O(clients), not O(requests)).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, db_url: str) -> PostgresKeyValueStore:
        """Create a store from a database URL (asyncpg driver expected)."""
        return cls(create_async_engine(db_url))

    async def close(self) -> None:
        await self._engine.dispose()

    def _coll(self, collection: str | None) -> str:
        return collection or _DEFAULT_COLLECTION

    def _live_or_none(self, row: _KVRow | None) -> _KVRow | None:
        if row is None:
            return None
        if row.expires_at is not None and row.expires_at.replace(tzinfo=UTC) < datetime.now(tz=UTC):
            return None
        return row

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = await session.get(_KVRow, (self._coll(collection), key))
        row = self._live_or_none(row)
        return dict(row.value) if row else None

    async def ttl(self, key: str, *, collection: str | None = None) -> tuple[dict[str, Any] | None, float | None]:
        async with self._session_factory() as session:
            row = await session.get(_KVRow, (self._coll(collection), key))
        row = self._live_or_none(row)
        if row is None:
            return None, None
        if row.expires_at is None:
            return dict(row.value), None
        remaining = (row.expires_at.replace(tzinfo=UTC) - datetime.now(tz=UTC)).total_seconds()
        return dict(row.value), max(0.0, remaining)

    async def put(
        self, key: str, value: Mapping[str, Any], *, collection: str | None = None, ttl: SupportsFloat | None = None
    ) -> None:
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=float(ttl)) if ttl is not None else None
        stmt = (
            pg_insert(_KVRow)
            .values(collection=self._coll(collection), key=key, value=dict(value), expires_at=expires_at)
            .on_conflict_do_update(
                index_elements=["collection", "key"], set_={"value": dict(value), "expires_at": expires_at}
            )
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        async with self._session_factory() as session:
            row = await session.get(_KVRow, (self._coll(collection), key))
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True

    # Bulk operations

    async def get_many(self, keys: Sequence[str], *, collection: str | None = None) -> list[dict[str, Any] | None]:
        coll = self._coll(collection)
        async with self._session_factory() as session:
            result = await session.execute(select(_KVRow).where(_KVRow.collection == coll, _KVRow.key.in_(keys)))
            rows = {r.key: r for r in result.scalars()}
        now = datetime.now(tz=UTC)
        return [
            dict(rows[k].value)
            if k in rows and ((exp := rows[k].expires_at) is None or exp.replace(tzinfo=UTC) >= now)
            else None
            for k in keys
        ]

    async def ttl_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        coll = self._coll(collection)
        async with self._session_factory() as session:
            result = await session.execute(select(_KVRow).where(_KVRow.collection == coll, _KVRow.key.in_(keys)))
            rows = {r.key: r for r in result.scalars()}
        now = datetime.now(tz=UTC)
        out: list[tuple[dict[str, Any] | None, float | None]] = []
        for k in keys:
            row = rows.get(k)
            if row is None:
                out.append((None, None))
                continue
            if row.expires_at is not None and row.expires_at.replace(tzinfo=UTC) < now:
                out.append((None, None))
                continue
            remaining = (
                None if row.expires_at is None else max(0.0, (row.expires_at.replace(tzinfo=UTC) - now).total_seconds())
            )
            out.append((dict(row.value), remaining))
        return out

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        for k, v in zip(keys, values, strict=True):
            await self.put(k, v, collection=collection, ttl=ttl)

    async def delete_many(self, keys: Sequence[str], *, collection: str | None = None) -> int:
        coll = self._coll(collection)
        async with self._session_factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(delete(_KVRow).where(_KVRow.collection == coll, _KVRow.key.in_(keys))),
            )
            await session.commit()
        return result.rowcount

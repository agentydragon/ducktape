"""Shared persistence configuration for MCP server state storage."""

from __future__ import annotations

from typing import Annotated, Literal, assert_never

from key_value.aio.stores.postgresql import PostgreSQLStore
from key_value.aio.stores.valkey import ValkeyStore
from pydantic import BaseModel, Field


class FilePersistence(BaseModel):
    kind: Literal["file"] = "file"


class ValkeyPersistence(BaseModel):
    kind: Literal["valkey"]
    host: str
    port: int = 6379
    db: int = 0


class PostgresPersistence(BaseModel):
    """Back the OAuth client-state store with a PostgreSQL table (py-key-value's `PostgreSQLStore`,
    JSONB via asyncpg). Suits a server that already runs Postgres and wants one fewer stateful
    dependency than a dedicated valkey. The store auto-creates its table on `setup()`."""

    kind: Literal["postgres"]
    # asyncpg DSN — `postgresql://user:pass@host:port/db`, NOT the SQLAlchemy `postgresql+psycopg://`
    # form an ORM uses (asyncpg rejects the `+driver` suffix).
    url: str
    table_name: str = "mcp_oauth_kv"


PersistenceConfig = Annotated[FilePersistence | ValkeyPersistence | PostgresPersistence, Field(discriminator="kind")]
SharedPersistenceConfig = Annotated[ValkeyPersistence | PostgresPersistence, Field(discriminator="kind")]
type OAuthClientStorage = ValkeyStore | PostgreSQLStore


def build_shared_client_storage(persistence: SharedPersistenceConfig) -> OAuthClientStorage:
    match persistence:
        case ValkeyPersistence(host=h, port=p, db=d):
            return ValkeyStore(host=h, port=p, db=d)
        case PostgresPersistence(url=u, table_name=t):
            return PostgreSQLStore(url=u, table_name=t)
        case _:
            assert_never(persistence)


def build_client_storage(persistence: PersistenceConfig) -> OAuthClientStorage | None:
    if isinstance(persistence, FilePersistence):
        return None
    return build_shared_client_storage(persistence)

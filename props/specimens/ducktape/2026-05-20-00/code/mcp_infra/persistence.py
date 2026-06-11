"""Shared persistence configuration for MCP server state storage."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from key_value.aio.stores.valkey import ValkeyStore
from pydantic import BaseModel, Field


class FilePersistence(BaseModel):
    kind: Literal["file"] = "file"


class ValkeyPersistence(BaseModel):
    kind: Literal["valkey"]
    host: str
    port: int = 6379
    db: int = 0


PersistenceConfig = Annotated[FilePersistence | ValkeyPersistence, Field(discriminator="kind")]


def build_client_storage(persistence: PersistenceConfig) -> Any:
    match persistence:
        case ValkeyPersistence(host=h, port=p, db=d):
            return ValkeyStore(host=h, port=p, db=d)
        case _:
            return None

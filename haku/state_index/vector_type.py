"""Minimal SQLAlchemy binding for pgvector's `vector` type.

The upstream `pgvector` Python package would do this and more, but the only things this index
needs are DDL for the column and a bind format for inserts — the one query that uses the
distance operator is raw SQL anyway (`store.search`). Twenty lines here keeps a new pip
dependency, and a lockfile regeneration, out of the change.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Dialect
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[Sequence[float]]):
    """pgvector's `vector`, with no dimension typmod — see `schema.Chunk.embedding`."""

    cache_ok = True

    def get_col_spec(self, **_kwargs: Any) -> str:
        return "vector"

    def bind_processor(self, dialect: Dialect) -> Any:
        def process(value: Sequence[float] | None) -> str | None:
            return None if value is None else f"[{','.join(map(str, value))}]"

        return process

    def result_processor(self, dialect: Dialect, coltype: Any) -> Any:
        def process(value: str | None) -> list[float] | None:
            return None if value is None else [float(part) for part in value.strip("[]").split(",")]

        return process

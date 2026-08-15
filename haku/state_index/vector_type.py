"""Minimal SQLAlchemy binding for pgvector's `halfvec` type.

The upstream `pgvector` Python package would do this and more, but the only things this index
needs are DDL for the column and a bind format for inserts — the one query that uses the
distance operator is raw SQL anyway (`store.search_git`/`search_chat`). Twenty lines here keeps a
new pip dependency, and a lockfile regeneration, out of the change.

**`halfvec`, not `vector`: half the bytes, and the only one of the two that this corpus could
ever index.** The embedding model returns 2560 dimensions, where a `vector` costs 4 bytes per
dimension (~10 KiB a chunk) and pgvector's HNSW/IVFFlat refuse anything over 2000; `halfvec` is
2 bytes per dimension (~5 KiB) and indexable to 4000. The cost is IEEE half precision — about
three decimal digits per component — which is noise next to what the embedding itself rounds off,
and these values are only ever compared, never read back and used.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Dialect
from sqlalchemy.types import UserDefinedType


class HalfVector(UserDefinedType[Sequence[float]]):
    """pgvector's `halfvec`, with no dimension typmod — see `schema.Chunk.embedding`."""

    cache_ok = True

    def get_col_spec(self, **_kwargs: Any) -> str:
        return "halfvec"

    # Writes only: no query selects an embedding back into Python. The one that touches the
    # vectors at all keeps them inside a CTE and returns a score, so a `result_processor` here
    # would be a code path nothing exercises.
    def bind_processor(self, dialect: Dialect) -> Any:
        def process(value: Sequence[float] | None) -> str | None:
            return None if value is None else f"[{','.join(map(str, value))}]"

        return process

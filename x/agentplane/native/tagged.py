"""A discriminator that routes unrecognized kinds to a named fallback model.

Both harnesses are the writers of their wire vocabularies and a pinned build can be newer than the
models here, so an unknown kind must decode to a named variant every reader dispatches on, never
fail validation and never be mistaken for a nearby member.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

UNKNOWN = "<unknown>"


def tag_or_unknown(field: str, known: frozenset[str]) -> Callable[[Any], str]:
    """A pydantic `Discriminator` callable: the value's `field` when it names a known kind, else the
    fallback tag, for a union whose last member is tagged `UNKNOWN`."""

    def tag(value: Any) -> str:
        kind = value.get(field) if isinstance(value, dict) else getattr(value, field, None)
        return kind if isinstance(kind, str) and kind in known else UNKNOWN

    return tag

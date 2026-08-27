"""Enum vocabularies that tolerate a newer writer's values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class UnknownValue:
    """A value none of a vocabulary's enums claims: what a reader older than its writer sees.

    A named variant rather than `None` or a nearby member. Consumers dispatch on it with
    `isinstance`, so a vocabulary that grows cannot be read as a value that already existed, and
    every reader is made to say what it does with a value it has no words for.

    Only a reader that opted into tolerance ever produces one — a stored column through
    `sqlalchemy_types.TolerantTextBackedStrEnumUnionColumn`, a payload through `member_or_unknown`.
    """

    value: str

    def __str__(self) -> str:
        return self.value


def member_or_unknown[E: StrEnum](enum_class: type[E], value: object) -> object:
    """Decode one wire value against *enum_class*'s vocabulary, tolerantly.

    The payload flavor of `sqlalchemy_types.TolerantTextBackedStrEnumUnionColumn`'s read side, for
    a cross-replica payload whose writer may be a newer release: a string the vocabulary does not
    claim becomes a named `UnknownValue` rather than a parse failure. Anything else — a member, an
    `UnknownValue` on re-validation, a non-string the caller's own validation will refuse — passes
    through unchanged. Shaped for a pydantic `mode="before"` validator on a `Member | UnknownValue`
    field.
    """
    return UnknownValue(value) if isinstance(value, str) and value not in enum_class else value

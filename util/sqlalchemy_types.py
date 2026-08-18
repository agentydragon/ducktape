"""Reusable SQLAlchemy column types."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Enum, String, Text
from sqlalchemy.types import TypeDecorator


@dataclass(frozen=True, slots=True)
class UnknownValue:
    """A stored value none of a column's enums claims: what a reader older than its writer sees.

    A named variant rather than `None` or a nearby member. Consumers dispatch on it with
    `isinstance`, so a vocabulary that grows cannot be read as a value that already existed, and
    every reader is made to say what it does with a value it has no words for.

    Only a column that opted into tolerance ever produces one — see
    `TolerantTextBackedStrEnumUnionColumn`.
    """

    value: str

    def __str__(self) -> str:
        return self.value


def _union_members(enum_classes: Sequence[type[StrEnum]]) -> dict[str, StrEnum]:
    """Every member of several enums by its stored spelling, refusing a value two of them claim.

    Overlap is refused because a reader could not then tell which category a row is in.
    """
    members: dict[str, StrEnum] = {}
    for enum_class in enum_classes:
        for member in enum_class:
            if (claimed := members.get(member.value)) is not None:
                raise ValueError(f"{member.value!r} is in both {type(claimed).__name__} and {enum_class.__name__}")
            members[member.value] = member
    return members


class StrEnumColumn[E: StrEnum](TypeDecorator[E]):
    """SQLAlchemy column type for Python ``StrEnum`` values backed by a DB enum."""

    impl = Enum
    cache_ok = True

    def __init__(self, enum_class: type[E], name: str):
        self._enum_class = enum_class
        super().__init__(*[e.value for e in enum_class], name=name, create_constraint=True, native_enum=True)

    def process_bind_param(self, value: E | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return value.value if isinstance(value, self._enum_class) else str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> E | None:
        if value is None:
            return None
        return self._enum_class(value)


class TextBackedStrEnumUnionColumn(TypeDecorator[StrEnum]):
    """``Text`` column whose members are drawn from more than one ``StrEnum``.

    Deviation from ``TextBackedStrEnumColumn``: the vocabulary is several enums rather than one,
    for a column that types one stream of values belonging to different categories. The
    vocabularies must not overlap — the constructor refuses a value two of them claim, since a
    reader could not then tell which category a row is in.
    """

    impl = Text
    cache_ok = True

    def __init__(self, *enum_classes: type[StrEnum]):
        self._members = _union_members(enum_classes)
        super().__init__()

    def process_bind_param(self, value: StrEnum | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> StrEnum | None:
        if value is None:
            return None
        return self._members[value]


class TolerantTextBackedStrEnumUnionColumn(TypeDecorator[StrEnum | UnknownValue]):
    """`TextBackedStrEnumUnionColumn` that decodes a value it does not know instead of raising.

    Deviation: `process_result_value` answers `UnknownValue` where the strict class raises
    `KeyError`. For a column whose writer may be a **newer release of this same process** — which
    under `maxUnavailable: 0` every stored vocabulary's writer may be — an unrecognised value is an
    expected state rather than a defect, and raising takes down a query over rows the reader does
    understand along with the one it does not.

    Opt-in, and the strict class remains the default: tolerance is only correct where ignoring a
    value is a defensible answer. Which columns those are, and why a value a reader must *act* on is
    not one of them, is <../haku/console/README.md> § Vocabularies across a roll.

    **Writing an `UnknownValue` back is refused.** A reader that cannot name a value has no business
    storing one, so laundering one through this column is a bug and fails where it happens.
    """

    impl = Text
    cache_ok = True

    def __init__(self, *enum_classes: type[StrEnum]):
        self._members = _union_members(enum_classes)
        super().__init__()

    def process_bind_param(self, value: StrEnum | UnknownValue | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, UnknownValue):
            raise ValueError(f"refusing to store a value this release cannot name: {value.value!r}")
        return str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> StrEnum | UnknownValue | None:
        if value is None:
            return None
        member = self._members.get(value)
        return UnknownValue(value) if member is None else member


class StringBackedStrEnumColumn[E: StrEnum](TypeDecorator[E]):
    """String column that returns a ``StrEnum`` in Python."""

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[E], length: int | None = None):
        self._enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: E | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return value.value if isinstance(value, self._enum_class) else str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> E | None:
        if value is None:
            return None
        return self._enum_class(value)


class TextBackedStrEnumColumn[E: StrEnum](TypeDecorator[E]):
    """``Text`` column that returns a ``StrEnum`` in Python.

    Deviation from ``StringBackedStrEnumColumn``: ``Text`` renders as ``TEXT`` where ``String``
    renders as ``VARCHAR``. Use this to type an existing ``Text`` column without changing its
    SQL type — ``compare_type`` in the schema tests treats the two as different, so swapping
    them is a migration, not a typing change.
    """

    impl = Text
    cache_ok = True

    def __init__(self, enum_class: type[E]):
        self._enum_class = enum_class
        super().__init__()

    def process_bind_param(self, value: E | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return value.value if isinstance(value, self._enum_class) else str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> E | None:
        if value is None:
            return None
        return self._enum_class(value)

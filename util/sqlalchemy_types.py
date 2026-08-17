"""Reusable SQLAlchemy column types."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import Enum, String, Text
from sqlalchemy.types import TypeDecorator


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
        self._members: dict[str, StrEnum] = {}
        for enum_class in enum_classes:
            for member in enum_class:
                if (claimed := self._members.get(member.value)) is not None:
                    raise ValueError(f"{member.value!r} is in both {type(claimed).__name__} and {enum_class.__name__}")
                self._members[member.value] = member
        super().__init__()

    def process_bind_param(self, value: StrEnum | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> StrEnum | None:
        if value is None:
            return None
        return self._members[value]


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

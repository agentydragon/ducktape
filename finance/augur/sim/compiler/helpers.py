"""Integer identifiers shared by execution-input tax compilation."""

from __future__ import annotations

NO_CODE = -1


class StringTable:
    def __init__(self) -> None:
        self._by_value: dict[str, int] = {}
        self.values: list[str] = []

    def intern(self, value: str | None) -> int:
        if value is None:
            return NO_CODE
        existing = self._by_value.get(value)
        if existing is not None:
            return existing
        code = len(self.values)
        self._by_value[value] = code
        self.values.append(value)
        return code

    def require(self, value: str) -> int:
        return self.intern(value)

"""Transport-independent correction-aware pagination for History session events."""

from __future__ import annotations

from collections.abc import Sequence

from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.testing.unstored_ranges import unstored_ranges
from agentplane.proto import event_log_pb2

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf


def covered_cursor(after_cursor: int, limit: int, page: history_service_pb2.ReadSessionEventsResponse) -> int:
    """Return the page's covered cursor, validating every entry or reported cursor gap.

    A full page covers only through its final entry. Its `last_cursor` is a global watermark and
    may include later entries that the page did not return.
    """
    if after_cursor < 0 or not 1 <= limit <= 1000:
        raise ValueError("invalid session history cursor or page limit")
    entries = list(page.entries)
    if len(entries) > limit or any(
        entry.cursor <= after_cursor or (index > 0 and entry.cursor <= entries[index - 1].cursor)
        for index, entry in enumerate(entries)
    ):
        raise ValueError("History Service returned unordered or out-of-window entries")
    if page.last_cursor < after_cursor or (entries and entries[-1].cursor > page.last_cursor):
        raise ValueError("History Service returned an invalid global cursor")

    end = entries[-1].cursor if len(entries) == limit else page.last_cursor
    if end <= after_cursor:
        return after_cursor

    intervals = sorted(
        [*((entry.cursor, entry.cursor) for entry in entries), *unstored_ranges(page)], key=lambda interval: interval[0]
    )
    cursor = after_cursor
    for first, last in intervals:
        if last <= cursor:
            continue
        if first > cursor + 1:
            raise ValueError(f"History page did not account for cursor {cursor + 1}")
        cursor = min(end, max(cursor, last))
        if cursor >= end:
            return cursor
    if cursor < end:
        raise ValueError(f"History page did not account for cursor {cursor + 1}")
    return cursor


class SessionEventPages:
    """Apply validated History pages while retaining corrections below the read cursor."""

    def __init__(self, *, after_cursor: int = 0, limit: int = 128) -> None:
        if after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid session history cursor or page limit")
        self.cursor = after_cursor
        self.limit = limit
        self.more_available = False
        self._entries: dict[int, event_log_pb2.EventEntry] = {}

    @property
    def entries(self) -> Sequence[event_log_pb2.EventEntry]:
        return tuple(self._entries[cursor] for cursor in sorted(self._entries))

    def apply_page(self, page: history_service_pb2.ReadSessionEventsResponse) -> list[event_log_pb2.EventEntry]:
        """Validate a page before applying corrections, entries, and cursor progress."""
        next_cursor = covered_cursor(self.cursor, self.limit, page)

        for first, last in unstored_ranges(page):
            for cursor in tuple(self._entries):
                if first <= cursor <= last:
                    del self._entries[cursor]
        newly_read = list(page.entries)
        for entry in newly_read:
            self._entries[entry.cursor] = entry

        self.cursor = next_cursor
        self.more_available = len(page.entries) == self.limit and self.cursor < page.last_cursor
        return newly_read

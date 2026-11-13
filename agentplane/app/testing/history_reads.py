"""Paged, correction-aware reads for App-authenticated Session history consumers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from http import HTTPStatus
from uuid import UUID

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import RECONNECT_PAUSE_SECONDS, RECONNECT_SECONDS, Client, _no_app_answered
from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.testing.unstored_ranges import unstored_ranges
from agentplane.proto import event_log_pb2

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf

HISTORY_POLL_SECONDS = 0.2
_TRANSPORT_FAILURES = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


class _RetryableHistoryReadError(Exception):
    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


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


class SessionEventReader:
    """Read typed History pages while retaining corrections reported below the read cursor.

    Callers evaluate their predicates after each page and keep their existing deadline around the
    loop. Archive readers stop at their previously captured committed watermark.
    """

    def __init__(self, client: Client, thread_id: UUID, *, after_cursor: int = 0, limit: int = 128) -> None:
        if after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid session history cursor or page limit")
        self.client = client
        self.thread_id = thread_id
        self.cursor = after_cursor
        self.limit = limit
        self.pending_attachment = False
        self.more_available = False
        self._entries: dict[int, event_log_pb2.EventEntry] = {}
        self._has_read = False

    @property
    def entries(self) -> Sequence[event_log_pb2.EventEntry]:
        return tuple(self._entries[cursor] for cursor in sorted(self._entries))

    async def read_page(self) -> list[event_log_pb2.EventEntry]:
        """Read one page, retrying only idempotent GETs during an App outage."""
        self.pending_attachment = False
        page = await self._read_page()
        if page is None:
            self.more_available = False
            return []

        for first, last in unstored_ranges(page):
            for cursor in tuple(self._entries):
                if first <= cursor <= last:
                    del self._entries[cursor]
        newly_read = list(page.entries)
        for entry in newly_read:
            self._entries[entry.cursor] = entry

        next_cursor = covered_cursor(self.cursor, self.limit, page)
        self.cursor = next_cursor
        self.more_available = len(page.entries) == self.limit and self.cursor < page.last_cursor
        return newly_read

    async def wait_for_next_page(self) -> None:
        """Poll after a quiet page, but continue paging immediately when its watermark is ahead."""
        if not self.more_available:
            await asyncio.sleep(HISTORY_POLL_SECONDS)

    async def _read_page(self) -> history_service_pb2.ReadSessionEventsResponse | None:
        try:
            return await AsyncRetrying(
                stop=stop_after_delay(RECONNECT_SECONDS),
                wait=wait_fixed(RECONNECT_PAUSE_SECONDS),
                retry=retry_if_exception_type(_RetryableHistoryReadError),
                reraise=True,
            )(self._read_page_once)
        except _RetryableHistoryReadError as error:
            raise TimeoutError(
                f"History read for Thread {self.thread_id} stayed unavailable after cursor {self.cursor}: {error.cause}"
            ) from error.cause

    async def _read_page_once(self) -> history_service_pb2.ReadSessionEventsResponse | None:
        try:
            page = await self.client.read_session_events(self.thread_id, after_cursor=self.cursor, limit=self.limit)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == HTTPStatus.NOT_FOUND:
                if self._has_read:
                    raise
                try:
                    await self.client.thread_metadata(self.thread_id)
                except httpx.HTTPStatusError as metadata_error:
                    if metadata_error.response.status_code == HTTPStatus.NOT_FOUND:
                        raise error
                    if not _no_app_answered(metadata_error.response):
                        raise
                    raise _RetryableHistoryReadError(metadata_error) from metadata_error
                except _TRANSPORT_FAILURES as metadata_error:
                    raise _RetryableHistoryReadError(metadata_error) from metadata_error
                # The app owns this Thread, but History has not attached its Session yet.
                self.pending_attachment = True
                return None
            if not _no_app_answered(error.response):
                raise
            raise _RetryableHistoryReadError(error) from error
        except _TRANSPORT_FAILURES as error:
            raise _RetryableHistoryReadError(error) from error

        # Every healthy History response, including an empty page, ends this retry window. The
        # caller's fixed operation deadline remains in force when no semantic progress is made.
        self._has_read = True
        return page

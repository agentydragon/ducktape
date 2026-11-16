"""Paged, correction-aware reads for App-authenticated Session history consumers."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from uuid import UUID

import grpc
import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import RECONNECT_PAUSE_SECONDS, RECONNECT_SECONDS, Client, _no_app_answered
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.testing.session_events import SessionEventPages
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


class SessionEventReader(SessionEventPages):
    """Read typed History pages while retaining corrections reported below the read cursor.

    Callers evaluate their predicates after each page and keep their existing deadline around the
    loop. Archive readers stop at their previously captured committed watermark.
    """

    def __init__(self, client: Client, thread_id: UUID, *, after_cursor: int = 0, limit: int = 128) -> None:
        super().__init__(after_cursor=after_cursor, limit=limit)
        self.client = client
        self.thread_id = thread_id
        self.pending_attachment = False
        self._has_read = False

    async def read_page(self) -> list[event_log_pb2.EventEntry]:
        """Read one page, retrying only idempotent GETs during an App outage."""
        self.pending_attachment = False
        page = await self._read_page()
        if page is None:
            self.more_available = False
            return []
        return self.apply_page(page)

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
                    await self.client.session(self.thread_id)
                except httpx.HTTPStatusError as session_error:
                    if session_error.response.status_code == HTTPStatus.NOT_FOUND:
                        raise error
                    if not _no_app_answered(session_error.response):
                        raise
                    raise _RetryableHistoryReadError(session_error) from session_error
                except _TRANSPORT_FAILURES as session_error:
                    raise _RetryableHistoryReadError(session_error) from session_error
                except ServiceError as session_error:
                    if session_error.code == grpc.StatusCode.NOT_FOUND:
                        raise error
                    if session_error.code != grpc.StatusCode.UNAVAILABLE:
                        raise
                    raise _RetryableHistoryReadError(session_error) from session_error
                # Dispatcher knows the Session, but History has not attached it yet.
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

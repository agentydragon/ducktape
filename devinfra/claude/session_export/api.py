"""Read-only client for claude.ai's private Claude Code session API (contract: docs/api.md)."""

import json
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
from httpx_sse import EventSource, ServerSentEvent, aconnect_sse
from more_itertools import one
from pydantic import BaseModel, SecretStr
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential
from tenacity.wait import wait_base

from devinfra.claude.session_export.failures import raise_for_status_with_body
from devinfra.claude.session_export.models import (
    DeliveryUpdate,
    Event,
    EventsPage,
    ResumeTokenPage,
    SessionRemoved,
    SessionsPage,
    SessionSummary,
    cse_id,
)
from devinfra.claude.session_export.oauth import OAuthTokenSource

logger = logging.getLogger(__name__)

CLAUDE_AI_URL = "https://claude.ai"
FIRST_PARTY_API_URL = "https://api.anthropic.com"
CCR_BETA = "ccr-byoc-2025-07-29"  # sent by the first-party clients; the list route works without it
EVENTS_PAGE_LIMIT = 500  # server maximum
SESSIONS_PAGE_LIMIT = 100
USER_AGENT = "claude-session-export/0.1 (personal data export)"  # the default python UA gets a Cloudflare challenge
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 6

# The web client restarts an event stream that has been silent this long, so the server sends something at
# least that often.
STREAM_IDLE_TIMEOUT = 35.0
# The server answers the watch `404 endpoint not enabled` unless the request names the web client's platform, as the
# web client's own does (docs/api.md § Session watch).
WATCH_CLIENT_PLATFORM = "web_claude_ai"
# The client sets no limit on the session watch. A connection that died without a close would leave the follower
# deaf until discovery next lists, so a silence this long reconnects.
WATCH_IDLE_TIMEOUT = 300.0
_STREAM_CONNECT_TIMEOUT = 30.0


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class EventStreamFrame(StrEnum):
    # A frame with no `event:` line. The web client ignores it beyond resetting its idle timer, and the server
    # sends one when a stream opens: a keepalive.
    UNNAMED = "message"
    CLIENT_EVENT = "client_event"
    CATCH_UP_TRUNCATED = "catch_up_truncated"
    SESSION_UPDATE = "session_update"
    EPHEMERAL_EVENT = "ephemeral_event"
    DELIVERY_UPDATE = "delivery_update"


class WatchFrame(StrEnum):
    # A frame with no `event:` line and no data, seen in the deployed sync's log as it connected: a keepalive.
    UNNAMED = "message"
    ADDED = "added"
    CHANGED = "changed"
    REMOVED = "removed"
    SYNC = "sync"


class ResumePointLostError(Exception):
    """The server cannot resume a stream from where it was asked to: page to catch up, then open it again."""


class StreamClosedEarlyError(Exception):
    """The server ended a stream without sending a frame."""


class SessionCookie(BaseModel, frozen=True):
    session_key: SecretStr
    org_uuid: str

    @classmethod
    def from_file(cls, path: Path) -> Self:
        """Reads a full `Cookie:` header value or just `sessionKey=...; lastActiveOrg=...`."""
        text = path.read_text()
        session_key = re.search(r"(?:^|[;\s])sessionKey=([^;\s]+)", text)
        org_uuid = re.search(r"(?:^|[;\s])lastActiveOrg=([^;\s]+)", text)
        if not (session_key and org_uuid):
            raise ValueError(f"{path} must contain sessionKey= and lastActiveOrg= cookies")
        return cls(session_key=SecretStr(session_key.group(1)), org_uuid=org_uuid.group(1))


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUSES
    return isinstance(exc, httpx.TransportError)


class BearerAuth(httpx.Auth):
    """Asks the token source for a valid access token on every request, so a long run outlives one token."""

    def __init__(self, tokens: OAuthTokenSource) -> None:
        self._tokens = tokens

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["authorization"] = f"Bearer {await self._tokens.access_token()}"
        yield request


class SessionsApi:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        *,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_wait: wait_base | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, auth=auth, timeout=120, transport=transport
        )
        self._retrying = AsyncRetrying(
            retry=retry_if_exception(_is_transient),
            wait=retry_wait or wait_exponential(multiplier=2, max=60),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            reraise=True,
        )
        self._unknown_frames: set[str] = set()

    @classmethod
    def for_cookie(
        cls,
        cookie: SessionCookie,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_wait: wait_base | None = None,
    ) -> Self:
        """Authenticate as the browser does: a `sessionKey` cookie against claude.ai."""
        headers = {
            "cookie": f"sessionKey={cookie.session_key.get_secret_value()}",
            "x-organization-uuid": cookie.org_uuid,
            "anthropic-version": "2023-06-01",
            "user-agent": USER_AGENT,
        }
        return cls(CLAUDE_AI_URL, headers, transport=transport, retry_wait=retry_wait)

    @classmethod
    def for_oauth(
        cls,
        tokens: OAuthTokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_wait: wait_base | None = None,
    ) -> Self:
        """Authenticate as the Claude Code CLI does: an OAuth bearer against the first-party API."""
        headers = {
            "x-organization-uuid": tokens.organization_uuid,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": CCR_BETA,
            "user-agent": USER_AGENT,
        }
        return cls(FIRST_PARTY_API_URL, headers, auth=BearerAuth(tokens), transport=transport, retry_wait=retry_wait)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._client.aclose()

    async def _get_once(self, path: str, params: dict[str, str | int]) -> httpx.Response:
        response = await self._client.get(path, params=params)
        raise_for_status_with_body(response)
        return response

    async def _get(self, path: str, **params: str | int | None) -> httpx.Response:
        return await self._retrying(self._get_once, path, {k: v for k, v in params.items() if v is not None})

    async def _sessions_page(self, *, limit: int, cursor: str | None = None) -> SessionsPage:
        # Claude Code Web's generic session-list client opts in to routine runs explicitly.
        response = await self._get("/v1/code/sessions", limit=limit, cursor=cursor, include_trigger_sessions="true")
        return SessionsPage.model_validate_json(response.content)

    async def list_sessions(self) -> AsyncIterator[SessionSummary]:
        cursor: str | None = None
        while True:
            page = await self._sessions_page(limit=SESSIONS_PAGE_LIMIT, cursor=cursor)
            for session in page.data:
                yield session
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    async def recent_sessions(self, count: int) -> list[SessionSummary]:
        """The `count` sessions with the newest events: the list is ordered by `last_event_at`, newest first."""
        return (await self._sessions_page(limit=min(count, SESSIONS_PAGE_LIMIT))).data

    async def iter_event_pages(self, session_id: str, *, after: int = 0) -> AsyncIterator[list[Event]]:
        """Pages of events with `sequence_num` above `after` (0: from the start), oldest first.

        Raises `ValueError` on a page that skips a `sequence_num`, before yielding it: a caller that stores pages
        as they come would leave an event past the gap that a resume from the newest stored never fetches.
        """
        position = after
        cursor = str(after) if after else None
        while True:
            response = await self._get(
                f"/v1/code/sessions/{session_id}/events",
                limit=EVENTS_PAGE_LIMIT,
                sort_order=SortOrder.ASC,
                cursor=cursor,
            )
            page = EventsPage.model_validate_json(response.content)
            for expected, event in enumerate(page.data, position + 1):
                if event.seq != expected:
                    raise ValueError(f"{session_id=}: expected sequence_num {expected}, got {event.sequence_num}")
            position += len(page.data)
            yield page.data
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    async def read_event(self, session_id: str, sequence_num: int) -> Event:
        """One event as the events route reports it now: its worker stamps move on after it is first sent."""
        response = await self._get(
            f"/v1/code/sessions/{session_id}/events",
            limit=1,
            sort_order=SortOrder.ASC,
            cursor=str(sequence_num - 1) if sequence_num > 1 else None,
        )
        event = one(EventsPage.model_validate_json(response.content).data)
        if event.seq != sequence_num:
            raise ValueError(f"{session_id=}: expected sequence_num {sequence_num}, got {event.sequence_num}")
        return event

    async def newest_sequence_num(self, session_id: str) -> int:
        response = await self._get(f"/v1/code/sessions/{session_id}/events", limit=1, sort_order=SortOrder.DESC)
        page = EventsPage.model_validate_json(response.content)
        return page.data[0].seq if page.data else 0

    async def resume_token(self) -> str:
        """Where the change feed stands now, as the web client probes it: a one-item list page."""
        response = await self._get("/v1/code/sessions", limit=1)
        return ResumeTokenPage.model_validate_json(response.content).resume_token

    @asynccontextmanager
    async def _open_stream(
        self, path: str, *, params: dict[str, str | int], headers: dict[str, str], idle_timeout: float
    ) -> AsyncIterator[EventSource]:
        timeout = httpx.Timeout(_STREAM_CONNECT_TIMEOUT, read=idle_timeout)
        async with aconnect_sse(self._client, "GET", path, params=params, headers=headers, timeout=timeout) as source:
            response = source.response
            if response.status_code == httpx.codes.GONE:
                raise ResumePointLostError(f"{path}: {response.status_code}")
            if response.is_error:
                await response.aread()
                raise_for_status_with_body(response)
            yield source

    def _note_unknown_frame(self, frame: ServerSentEvent) -> None:
        """Log the first frame of each unknown kind: its size and, for a JSON object, its keys, never its values."""
        if frame.event in self._unknown_frames:
            return
        self._unknown_frames.add(frame.event)
        try:
            document = json.loads(frame.data)
        except ValueError:
            shape = "not JSON"
        else:
            shape = f"JSON with keys {sorted(document)}" if isinstance(document, dict) else "JSON, not an object"
        logger.warning(
            "ignoring server-sent frame of an unknown kind: %s, %d bytes of data, %s",
            frame.event,
            len(frame.data),
            shape,
        )

    async def stream_events(
        self, session_id: str, *, after: int, on_connected: Callable[[], None] | None = None
    ) -> AsyncGenerator[Event | DeliveryUpdate]:
        """The live tail of a session's events past `after`, each as the server pushes it, and the delivery updates.

        `on_connected` is called once the server has accepted the stream. Ends when the server closes the stream or
        it falls silent for `STREAM_IDLE_TIMEOUT`. Raises `ResumePointLostError` when the server cannot resume from
        `after`, and `StreamClosedEarlyError` when it closes without a frame. From `after=0` the web client sends
        no resume position either.
        """
        resume_from: dict[str, str | int] = {"from_sequence_num": after} if after else {}
        delivered = False
        try:
            async with self._open_stream(
                f"/v1/code/sessions/{cse_id(session_id)}/events/stream",
                params=resume_from,
                headers={"last-event-id": str(after)} if after else {},
                idle_timeout=STREAM_IDLE_TIMEOUT,
            ) as source:
                if on_connected:
                    on_connected()
                async for frame in source.aiter_sse():
                    delivered = True
                    match frame.event:
                        case EventStreamFrame.CLIENT_EVENT:
                            if frame.data:  # an empty one only advances the client's cursor
                                yield Event.model_validate_json(frame.data)
                        case EventStreamFrame.DELIVERY_UPDATE:
                            if frame.data:
                                yield DeliveryUpdate.model_validate_json(frame.data)
                        case EventStreamFrame.CATCH_UP_TRUNCATED:
                            raise ResumePointLostError(
                                f"{session_id=}: the server truncated the catch-up after {after}"
                            )
                        case (
                            EventStreamFrame.UNNAMED
                            | EventStreamFrame.SESSION_UPDATE
                            | EventStreamFrame.EPHEMERAL_EVENT
                        ):
                            pass  # nothing the store keeps: a keepalive, not persisted, or metadata the list carries
                        case _:
                            self._note_unknown_frame(frame)
        except httpx.ReadTimeout:
            return
        if not delivered:
            raise StreamClosedEarlyError(f"{session_id=}: the server closed the stream without a frame")

    async def watch_sessions(
        self, resume_token: str, *, on_connected: Callable[[], None] | None = None
    ) -> AsyncGenerator[SessionSummary | SessionRemoved]:
        """Changes to the account's sessions since `resume_token`, as the server pushes them.

        The token must be fresh: the server accepts an old one and then delivers nothing (docs/api.md § Session
        watch). `on_connected` is called once the server has accepted the stream. Ends when the server closes the
        stream or it falls silent for `WATCH_IDLE_TIMEOUT`. Raises `ResumePointLostError` when the server no longer
        holds `resume_token`, and `StreamClosedEarlyError` when it closes without a frame.
        """
        delivered = False
        try:
            async with self._open_stream(
                "/v1/code/sessions/watch",
                params={"exclude_tags": "-", "resume_token": resume_token},
                headers={"anthropic-client-platform": WATCH_CLIENT_PLATFORM},
                idle_timeout=WATCH_IDLE_TIMEOUT,
            ) as source:
                if on_connected:
                    on_connected()
                async for frame in source.aiter_sse():
                    delivered = True
                    match frame.event:
                        case WatchFrame.ADDED | WatchFrame.CHANGED:
                            if frame.data:
                                yield SessionSummary.model_validate_json(frame.data)
                        case WatchFrame.REMOVED:
                            if frame.data:
                                yield SessionRemoved.model_validate_json(frame.data)
                        case WatchFrame.SYNC:
                            pass  # the feed has delivered everything up to the token the watch opened with
                        case WatchFrame.UNNAMED if not frame.data:
                            pass  # a keepalive
                        case _:
                            self._note_unknown_frame(frame)
        except httpx.ReadTimeout:
            return
        if not delivered:
            raise StreamClosedEarlyError("the session watch closed without a frame")

"""Read-only client for claude.ai's private Claude Code session API (contract: docs/api.md)."""

import re
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, SecretStr
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential
from tenacity.wait import wait_base

from devinfra.claude.session_export.models import Event, EventsPage, SessionsPage, SessionSummary

BASE_URL = "https://claude.ai"
EVENTS_PAGE_LIMIT = 500  # server maximum
SESSIONS_PAGE_LIMIT = 100
USER_AGENT = "claude-session-export/0.1 (personal data export)"  # the default python UA gets a Cloudflare challenge
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 6


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


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


class SessionsApi:
    def __init__(
        self,
        cookie: SessionCookie,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_wait: wait_base | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "cookie": f"sessionKey={cookie.session_key.get_secret_value()}",
                "x-organization-uuid": cookie.org_uuid,
                "anthropic-version": "2023-06-01",
                "user-agent": USER_AGENT,
            },
            timeout=120,
            transport=transport,
        )
        self._retrying = AsyncRetrying(
            retry=retry_if_exception(_is_transient),
            wait=retry_wait or wait_exponential(multiplier=2, max=60),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            reraise=True,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._client.aclose()

    async def _get_once(self, path: str, params: dict[str, str | int]) -> httpx.Response:
        response = await self._client.get(path, params=params)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            e.add_note(response.text[:300])  # the API's error body says why (missing header, expired key)
            raise
        return response

    async def _get(self, path: str, **params: str | int | None) -> httpx.Response:
        return await self._retrying(self._get_once, path, {k: v for k, v in params.items() if v is not None})

    async def list_sessions(self) -> AsyncIterator[SessionSummary]:
        cursor: str | None = None
        while True:
            response = await self._get("/v1/code/sessions", limit=SESSIONS_PAGE_LIMIT, cursor=cursor)
            page = SessionsPage.model_validate_json(response.content)
            for session in page.data:
                yield session
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    async def iter_event_pages(self, session_id: str) -> AsyncIterator[list[Event]]:
        cursor: str | None = None
        while True:
            response = await self._get(
                f"/v1/code/sessions/{session_id}/events",
                limit=EVENTS_PAGE_LIMIT,
                sort_order=SortOrder.ASC,
                cursor=cursor,
            )
            page = EventsPage.model_validate_json(response.content)
            yield page.data
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    async def newest_sequence_num(self, session_id: str) -> int:
        response = await self._get(f"/v1/code/sessions/{session_id}/events", limit=1, sort_order=SortOrder.DESC)
        page = EventsPage.model_validate_json(response.content)
        return page.data[0].seq if page.data else 0

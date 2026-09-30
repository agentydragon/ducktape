import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tenacity import wait_none

from devinfra.claude.session_export.api import SessionCookie, SessionsApi
from devinfra.claude.session_export.oauth import OAuthCredential

TEST_COOKIE = SessionCookie(session_key=SecretStr("test-session-key"), org_uuid="test-org-uuid")
TEST_ACCESS_TOKEN = "test-access-token"


def make_credential(*, expires_in: timedelta = timedelta(hours=1)) -> OAuthCredential:
    return OAuthCredential(
        access_token=SecretStr(TEST_ACCESS_TOKEN),
        refresh_token=SecretStr("test-refresh-1"),
        expires_at=datetime.now(UTC) + expires_in,
        scopes=frozenset({"user:profile"}),
        organization_uuid=TEST_COOKIE.org_uuid,
    )


def make_events(count: int) -> list[dict[str, Any]]:
    return [
        {
            "event_id": f"test-event-{seq}",
            "event_type": "user" if seq % 2 else "assistant",
            "sequence_num": str(seq),
            "payload": {"text": f"héllo {seq}", "nested": {"n": seq}},
        }
        for seq in range(1, count + 1)
    ]


@dataclass
class FakeSessionsService:
    """In-memory claude.ai session API following the contract in docs/api.md."""

    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    fail_next: list[int] = field(default_factory=list)  # HTTP statuses answered before any real response
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0))
        cookie_ok = "sessionKey=test-session-key" in request.headers.get("cookie", "")
        bearer_ok = request.headers.get("authorization") == f"Bearer {TEST_ACCESS_TOKEN}"
        if not (cookie_ok or bearer_ok) or request.headers["x-organization-uuid"] != TEST_COOKIE.org_uuid:
            return httpx.Response(401, json={"error": {"type": "authentication_error"}})
        params = request.url.params
        limit = int(params["limit"])
        if not 1 <= limit <= (500 if request.url.path.endswith("/events") else 100):
            return httpx.Response(400, json={"error": {"message": "limit out of range"}})
        cursor = int(params.get("cursor", 0))
        if request.url.path == "/v1/code/sessions":
            ids = list(self.events)
            listed = ids[cursor : cursor + limit]
            sessions_body: dict[str, Any] = {"data": [{"id": i, "last_event_at": f"test-time-{i}"} for i in listed]}
            if cursor + limit < len(ids):
                sessions_body["next_cursor"] = str(cursor + limit)
            return httpx.Response(200, json=sessions_body)
        session_id = re.fullmatch(r"/v1/code/sessions/([^/]+)/events", request.url.path)
        assert session_id, request.url.path
        events = self.events[session_id[1]]
        if params["sort_order"] == "asc":
            page = [e for e in events if int(e["sequence_num"]) > cursor][:limit]
            more = bool(page) and page[-1] is not events[-1]
        else:
            page = [e for e in reversed(events) if not cursor or int(e["sequence_num"]) < cursor][:limit]
            more = bool(page) and page[-1] is not events[0]
        events_body: dict[str, Any] = {"data": page, "resume_cursor": page[-1]["sequence_num"] if page else str(cursor)}
        if more:
            events_body["next_cursor"] = page[-1]["sequence_num"]
        return httpx.Response(200, json=events_body)


@pytest.fixture
def service() -> FakeSessionsService:
    return FakeSessionsService()


@pytest.fixture
async def api(service: FakeSessionsService) -> AsyncIterator[SessionsApi]:
    transport = httpx.MockTransport(service.handle)
    async with SessionsApi.for_cookie(TEST_COOKIE, transport=transport, retry_wait=wait_none()) as api:
        yield api

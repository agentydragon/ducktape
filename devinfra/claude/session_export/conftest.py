import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from tenacity import wait_none
from testcontainers.postgres import PostgresContainer

from devinfra.claude.session_export.api import SessionCookie, SessionsApi
from devinfra.claude.session_export.database_migrate import RUNNER
from devinfra.claude.session_export.models import DEFAULT_ATTESTATION_STATUS, SessionSummary
from devinfra.claude.session_export.oauth import OAuthCredential
from devinfra.claude.session_export.store import SessionStore, make_engine
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container

TEST_COOKIE = SessionCookie(session_key=SecretStr("test-session-key"), org_uuid="test-org-uuid")
TEST_ACCESS_TOKEN = "test-access-token"
TEST_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def make_credential(*, expires_in: timedelta = timedelta(hours=1)) -> OAuthCredential:
    return OAuthCredential(
        access_token=SecretStr(TEST_ACCESS_TOKEN),
        refresh_token=SecretStr("test-refresh-1"),
        expires_at=datetime.now(UTC) + expires_in,
        scopes=frozenset({"user:profile"}),
        organization_uuid=TEST_COOKIE.org_uuid,
    )


def make_session(
    session_id: str, *, title: str = "Test session", last_event_at: str = TEST_EPOCH.isoformat()
) -> SessionSummary:
    return SessionSummary(
        id=session_id,
        title=title,
        status="archived",
        created_at=TEST_EPOCH.isoformat(),
        updated_at=TEST_EPOCH.isoformat(),
        last_event_at=last_event_at,
    )


def make_event(seq: int, **fields: Any) -> dict[str, Any]:
    """An event as the API sends the ones that never pass through the worker's queue: no processing stamps."""
    return {
        "event_id": str(UUID(int=seq)),
        "event_type": "user" if seq % 2 else "assistant",
        "source": "worker",
        "sequence_num": str(seq),
        "created_at": (TEST_EPOCH + timedelta(seconds=seq)).isoformat(),
        "device_attestation_status": DEFAULT_ATTESTATION_STATUS,
        "sent_by_account_id": None,
        "payload": {"text": f"héllo {seq}", "nested": {"n": seq}},
        **fields,
    }


def make_events(count: int) -> list[dict[str, Any]]:
    return [make_event(seq) for seq in range(1, count + 1)]


@dataclass
class FakeSessionsService:
    """In-memory claude.ai session API following the contract in docs/api.md."""

    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)  # overrides the generated title of a listed session
    fail_next: list[int] = field(default_factory=list)  # HTTP statuses answered before any real response
    requests: list[httpx.Request] = field(default_factory=list)

    def list_item(self, session_id: str) -> dict[str, Any]:
        events = self.events[session_id]
        last_event_at = events[-1]["created_at"] if events else TEST_EPOCH.isoformat()
        title = self.titles.get(session_id, "Test session")
        return make_session(session_id, title=title, last_event_at=last_event_at).model_dump()

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
            sessions_body: dict[str, Any] = {"data": [self.list_item(i) for i in listed]}
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


@pytest.fixture
def database_url(postgres_container: PostgresContainer) -> Iterator[str]:
    admin_url = (
        f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}"
        f":{postgres_container.get_exposed_port(5432)}/postgres"
    )
    name = f"sessions_{uuid4().hex}"
    url = create_database_sync(admin_url, name)
    RUNNER.apply(url)
    yield url
    force_drop_database_sync(admin_url, name)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def store(engine: AsyncEngine) -> SessionStore:
    return SessionStore(engine)

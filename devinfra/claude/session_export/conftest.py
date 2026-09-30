import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed, wait_none
from testcontainers.postgres import PostgresContainer

from devinfra.claude.session_export.api import SessionCookie, SessionsApi
from devinfra.claude.session_export.database_migrate import RUNNER
from devinfra.claude.session_export.models import DEFAULT_ATTESTATION_STATUS, SESSION_STATUS_ARCHIVED, SessionSummary
from devinfra.claude.session_export.oauth import CredentialStore, OAuthCredential
from devinfra.claude.session_export.store import SessionStore, make_engine
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container

TEST_ORG_UUID = "test-org-uuid"
TEST_COOKIE = SessionCookie(session_key=SecretStr("test-session-key"), org_uuid=TEST_ORG_UUID)
TEST_ACCESS_TOKEN = "test-access-token"
TEST_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
LIVE_WINDOW = timedelta(days=36500)  # every test session's last event counts as recent
RESUME_TOKEN = "test-resume-token"
ONE, TWO, EMPTY = "session_test0001", "session_test0002", "session_test0003"  # `EMPTY` is seeded without events
# Statuses the server sends. Plain strings, not an enum: the server may add more.
SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_PAUSED = "paused"

PAIRED_RESPONSE = {
    "access_token": TEST_ACCESS_TOKEN,
    "refresh_token": "test-refresh-1",
    "expires_in": 3600,
    "scope": "user:profile",
    "organization": {"uuid": TEST_ORG_UUID},
}


class FakeTokenEndpoint:
    """Answers every request with `response`, or with `status` and no body when that is not 200."""

    def __init__(self, response: dict[str, Any], *, status: int = 200) -> None:
        self.response = response
        self.status = status
        self.bodies: list[dict[str, str]] = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(self.status, json=self.response)


def make_credential(*, expires_in: timedelta = timedelta(hours=1)) -> OAuthCredential:
    return OAuthCredential(
        access_token=SecretStr(TEST_ACCESS_TOKEN),
        refresh_token=SecretStr("test-refresh-1"),
        expires_at=datetime.now(UTC) + expires_in,
        scopes=frozenset({"user:profile"}),
        organization_uuid=TEST_ORG_UUID,
    )


def make_session(
    session_id: str,
    *,
    title: str = "Test session",
    status: str = SESSION_STATUS_ARCHIVED,
    last_event_at: str = TEST_EPOCH.isoformat(),
) -> SessionSummary:
    return SessionSummary(
        id=session_id,
        title=title,
        status=status,
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


class SseBody(httpx.AsyncByteStream):
    def __init__(self, frames: asyncio.Queue[bytes | None]) -> None:
        self._frames = frames

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (chunk := await self._frames.get()) is not None:
            yield chunk


@dataclass
class SseConnection:
    """One opened server-sent-event stream, which the test feeds and ends."""

    request: httpx.Request
    frames: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)

    def send(self, event: str | None, data: dict[str, Any] | None = None, *, frame_id: str | None = None) -> None:
        """`event=None` sends a frame with no `event:` line, as the server's keepalive does."""
        lines = [] if event is None else [f"event: {event}"]
        if frame_id:
            lines.append(f"id: {frame_id}")
        if data is not None:
            lines.append(f"data: {json.dumps(data)}")
        self.frames.put_nowait(("\n".join(lines) + "\n\n").encode())

    def close(self) -> None:
        self.frames.put_nowait(None)


@dataclass
class FakeSessionsService:
    """In-memory claude.ai session API following the contract in docs/api.md."""

    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)  # overrides the generated title of a listed session
    statuses: dict[str, str] = field(default_factory=dict)  # overrides the generated status (archived)
    fail_next: list[int] = field(default_factory=list)  # HTTP statuses answered before any real response
    stream_refusals: list[int] = field(default_factory=list)  # HTTP statuses answered to the next stream opens
    list_status: int | None = None  # answers every session list request with this status while set
    requests: list[httpx.Request] = field(default_factory=list)
    streams: list[SseConnection] = field(default_factory=list)  # every stream opened, in order
    on_open: list[Callable[[SseConnection], None]] = field(default_factory=list)  # scripts the next stream opens

    def list_item(self, session_id: str) -> dict[str, Any]:
        events = self.events[session_id]
        last_event_at = events[-1]["created_at"] if events else TEST_EPOCH.isoformat()
        title = self.titles.get(session_id, "Test session")
        status = self.statuses.get(session_id, SESSION_STATUS_ARCHIVED)
        return make_session(session_id, title=title, status=status, last_event_at=last_event_at).model_dump()

    def streams_at(self, path: str) -> list[SseConnection]:
        return [s for s in self.streams if s.request.url.path == path]

    def event_streams(self, session_id: str) -> list[SseConnection]:
        return self.streams_at(f"/v1/code/sessions/cse_{session_id.removeprefix('session_')}/events/stream")

    def watches(self) -> list[SseConnection]:
        return self.streams_at("/v1/code/sessions/watch")

    def event_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/events")]

    def event_reads(self) -> list[httpx.Request]:
        """The oldest-first page reads, not the newest-first probes of a session's newest `sequence_num`."""
        return [r for r in self.event_requests() if r.url.params["sort_order"] == "asc"]

    def _open_stream(self, request: httpx.Request) -> httpx.Response:
        if self.stream_refusals:
            return httpx.Response(self.stream_refusals.pop(0), json={"error": {"type": "refused"}})
        connection = SseConnection(request)
        self.streams.append(connection)
        if self.on_open:
            self.on_open.pop(0)(connection)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SseBody(connection.frames))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0))
        cookie_ok = "sessionKey=test-session-key" in request.headers.get("cookie", "")
        bearer_ok = request.headers.get("authorization") == f"Bearer {TEST_ACCESS_TOKEN}"
        if not (cookie_ok or bearer_ok) or request.headers["x-organization-uuid"] != TEST_ORG_UUID:
            return httpx.Response(401, json={"error": {"type": "authentication_error"}})
        if request.url.path == "/v1/code/sessions/watch" and "anthropic-client-platform" not in request.headers:
            return httpx.Response(404, text="endpoint not enabled\n")  # what the server says without the header
        if request.url.path.endswith(("/events/stream", "/sessions/watch")):
            return self._open_stream(request)
        if self.list_status and request.url.path == "/v1/code/sessions":
            return httpx.Response(self.list_status, json={"error": {"type": "refused"}})
        params = request.url.params
        limit = int(params["limit"])
        if not 1 <= limit <= (500 if request.url.path.endswith("/events") else 100):
            return httpx.Response(400, json={"error": {"message": "limit out of range"}})
        cursor = int(params.get("cursor", 0))
        if request.url.path == "/v1/code/sessions":
            ids = list(self.events)
            listed = ids[cursor : cursor + limit]
            sessions_body: dict[str, Any] = {"data": [self.list_item(i) for i in listed], "resume_token": RESUME_TOKEN}
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


def authorization_state(authorization_url: str) -> str:
    """The `state` the authorization URL carries, which the redirect has to bring back."""
    return dict(parse_qsl(urlsplit(authorization_url).query))["state"]


def redirect_url(state: str) -> str:
    """The URL the browser is sent to after approval, as the human pastes it back."""
    return f"http://localhost:54545/callback?code=test-code&state={state}"


@asynccontextmanager
async def background(coroutine: Coroutine[Any, Any, None]) -> AsyncIterator[asyncio.Task[None]]:
    """Runs `coroutine` as a task for the block, then cancels it and waits it out; the cancellation is not an error."""
    task = asyncio.create_task(coroutine)
    try:
        yield task
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def eventually(condition: Callable[[], Awaitable[bool]]) -> None:
    """Returns once `condition` holds: the wait for a background loop's progress, bounded so a wedge fails the test."""
    async for attempt in AsyncRetrying(wait=wait_fixed(0.01), stop=stop_after_delay(30), reraise=True):
        with attempt:
            assert await condition()


@pytest.fixture
def service() -> FakeSessionsService:
    return FakeSessionsService()


@pytest.fixture
async def api(service: FakeSessionsService) -> AsyncIterator[SessionsApi]:
    transport = httpx.MockTransport(service.handle)
    async with SessionsApi.for_cookie(TEST_COOKIE, transport=transport, retry_wait=wait_none()) as api:
        yield api


@pytest.fixture
def credential_store(tmp_path: Path) -> Callable[..., CredentialStore]:
    """Saves a credential that expires in `expires_in` and returns the store holding it."""

    def make(*, expires_in: timedelta = timedelta(hours=1)) -> CredentialStore:
        store = CredentialStore(tmp_path / "credential.json")
        store.save(make_credential(expires_in=expires_in))
        return store

    return make


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

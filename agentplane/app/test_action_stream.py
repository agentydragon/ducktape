"""`/actions/stream` behind the real session middleware, over an HTTPX upstream the test scripts.

The browser's side is driven straight through the ASGI app, because httpx's own ASGI transport would
buffer a body that never ends; logins and logouts go through httpx. Another replica is a second app
whose operator sessions are on a connection pool of their own, and whose listener never starts.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from sqlalchemy.engine import make_url
from starlette.types import Message, Scope

from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.runner.bridge import RunnerBridge
from agentplane.app.agent_runtime.thread.store import ThreadStore
from agentplane.app.agent_runtime.view.content import ContentStore
from agentplane.app.api import create_app
from agentplane.app.conftest import Replica
from agentplane.app.database_updates import Channel, DatabaseUpdates
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress import EgressInventory
from agentplane.app.inventory import SandboxInventory
from agentplane.app.live import LiveIndex
from agentplane.app.oidc import INSECURE_COOKIE, OIDCSettings
from agentplane.app.operator_sessions import OperatorSession, OperatorSessionStore, request_session
from agentplane.app.presets import Harness

APP_URL = "http://test-app.invalid"
OIDC = OIDCSettings(
    issuer="https://test-idp.invalid/",
    client_id="test-app",
    client_secret="test-client-secret",  # a test literal, not a real credential
    session_secret="test-session-secret",  # a test literal, not a real credential
    public_base_url=APP_URL,
)
FIRST = b"event: snapshot\ndata: first\n\n"
SECOND = b"event: snapshot\ndata: second\n\n"


class StreamActions(FederatedOperatorActions):
    def __init__(self, client: OperatorActionServiceClient) -> None:
        self.client = client

    def for_request(self, request: Request) -> OperatorActionServiceClient:
        return self.client


class Tokens:
    """A new token per exchange, as the federation mints one."""

    def __init__(self) -> None:
        self.issued = 0

    async def token(self) -> str:
        self.issued += 1
        return f"test-token-{self.issued}"


class Upstream(httpx.AsyncByteStream):
    """One upstream response body: whatever is put in `frames`, in order, waiting when it runs dry.
    An exception is raised where it stands, and None ends the body cleanly."""

    def __init__(self, *frames: bytes | Exception | None) -> None:
        self.frames: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (frame := await self.frames.get()) is not None:
            if isinstance(frame, Exception):
                raise frame
            yield frame

    async def aclose(self) -> None:
        self.closed = True


class ActionService:
    """Answers each stream request with the next of `upstreams`, recording the bearer it came with."""

    def __init__(self, *upstreams: Upstream, status_code: int = 200) -> None:
        self.upstreams = upstreams
        self.status_code = status_code
        self.authorizations: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def answer(request: httpx.Request) -> httpx.Response:
            self.authorizations.append(request.headers["Authorization"])
            return httpx.Response(self.status_code, stream=self.upstreams[len(self.authorizations) - 1])

        return httpx.MockTransport(answer)


@asynccontextmanager
async def operator_actions(app: FastAPI, transport: httpx.AsyncBaseTransport) -> AsyncIterator[None]:
    """`app`'s operator Action client, over `transport`, for as long as the block runs."""
    async with httpx.AsyncClient(base_url="https://test-actions.invalid", transport=transport) as http:
        app.state.operator_actions = StreamActions(OperatorActionServiceClient(http, Tokens()))
        yield


class EventSource:
    """The browser's `/actions/stream` request, carrying `cookie`."""

    def __init__(self, app: FastAPI, cookie: str) -> None:
        self.messages: list[Message] = []
        self.chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._disconnected = asyncio.Event()
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/actions/stream",
            "query_string": b"",
            "headers": [(b"cookie", f"{INSECURE_COOKIE}={cookie}".encode())],
        }
        self._served = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self) -> Message:
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        self.messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.chunks.put_nowait(message["body"])

    async def next_chunk(self) -> bytes:
        async with asyncio.timeout(10):
            return await self.chunks.get()

    def disconnect(self) -> None:
        self._disconnected.set()

    async def ended(self) -> list[Message]:
        async with asyncio.timeout(10):
            await self._served
        return self.messages


# The app `create_app` builds, over one replica's operator sessions and listener, with a login that
# needs no identity provider.
ServeApp = Callable[[OperatorSessionStore, DatabaseUpdates], FastAPI]


@pytest.fixture
def serve(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: ThreadStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    event_logs: EventLogStore,
    content: ContentStore,
) -> ServeApp:
    def serving(sessions: OperatorSessionStore, updates: DatabaseUpdates) -> FastAPI:
        app = create_app(
            inventory,
            bridge,
            store,
            {harness: ["test-model"] for harness in Harness},
            egress,
            decisions,
            live_index,
            action_policy,
            OIDC,
            event_logs=event_logs,
            content=content,
            database_updates=updates,
            operator_sessions=sessions,
        )

        @app.post("/test-login")
        async def login(request: Request, seconds: float) -> None:
            request_session(request).log_in(
                OperatorSession(issuer=OIDC.issuer, subject="test-subject", username="test-operator"),
                absolute_expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
            )

        return app

    return serving


@pytest.fixture
def app(serve: ServeApp, operator_sessions: OperatorSessionStore, database_updates: DatabaseUpdates) -> FastAPI:
    return serve(operator_sessions, database_updates)


@pytest.fixture
def other_replica(serve: ServeApp, replica: Replica, db_url: str) -> FastAPI:
    return serve(replica.operator_sessions, DatabaseUpdates(make_url(db_url)))


async def login(app: FastAPI, *, seconds: float = 3600) -> str:
    """A new operator session's cookie."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_URL) as browser:
        (await browser.post("/test-login", params={"seconds": seconds})).raise_for_status()
        return browser.cookies[INSECURE_COOKIE]


async def logout(app: FastAPI, cookie: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
    ) as browser:
        response = await browser.post("/auth/logout", headers={"Origin": APP_URL})
        assert response.status_code == 303, response.text


@pytest.mark.parametrize("upstream_status", [401, 403, 503])
async def test_upstream_status_before_downstream_headers(app: FastAPI, upstream_status: int) -> None:
    service = ActionService(Upstream(FIRST), status_code=upstream_status)
    cookie = await login(app)
    async with operator_actions(app, service.transport()):
        messages = await EventSource(app, cookie).ended()
    assert messages[0]["status"] == upstream_status
    assert service.upstreams[0].closed


async def test_connect_error_before_downstream_headers(app: FastAPI) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test connection failed", request=request)

    cookie = await login(app)
    async with operator_actions(app, httpx.MockTransport(fail)):
        messages = await EventSource(app, cookie).ended()
    assert messages[0]["status"] == 503


@pytest.mark.parametrize("ending", ["reset", "disconnect", "drain"])
async def test_stream_cleanup(app: FastAPI, ending: str, caplog: pytest.LogCaptureFixture) -> None:
    upstream = Upstream(FIRST, httpx.ReadError("test upstream reset")) if ending == "reset" else Upstream(FIRST)
    service = ActionService(upstream)
    cookie = await login(app)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie)
        assert await browser.next_chunk() == FIRST
        if ending == "disconnect":
            browser.disconnect()
        elif ending == "drain":
            app.state.drain.begin()
        messages = await browser.ended()
    assert messages[0]["status"] == 200
    assert upstream.closed
    assert ("Action stream interrupted" in caplog.text) == (ending == "reset")


async def test_a_logout_on_another_replica_ends_the_stream(app: FastAPI, other_replica: FastAPI) -> None:
    service = ActionService(Upstream(FIRST))
    cookie = await login(app)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie)
        assert await browser.next_chunk() == FIRST
        await logout(other_replica, cookie)
        await browser.ended()
    assert service.upstreams[0].closed


async def test_another_sessions_logout_leaves_the_stream_open(
    app: FastAPI, other_replica: FastAPI, database_updates: DatabaseUpdates
) -> None:
    upstream = Upstream(FIRST)
    service = ActionService(upstream)
    cookie, other = await login(app), await login(app)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie)
        assert await browser.next_chunk() == FIRST
        heard = asyncio.Event()
        with database_updates.changes[Channel.OPERATOR_SESSIONS].subscribe(heard):
            await logout(other_replica, other)
            await asyncio.wait_for(heard.wait(), timeout=10)
        # The stream's own reader woke on that same notification; a frame after it still arrives.
        upstream.frames.put_nowait(SECOND)
        assert await browser.next_chunk() == SECOND
        await logout(other_replica, cookie)
        await browser.ended()
    assert upstream.closed


async def test_the_stream_ends_when_its_session_expires(app: FastAPI) -> None:
    service = ActionService(Upstream(FIRST))
    # Long enough to open the stream inside it; nothing but the expiry ends this one.
    cookie = await login(app, seconds=2)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie)
        assert await browser.next_chunk() == FIRST
        await browser.ended()
    assert service.upstreams[0].closed


async def test_an_upstream_ended_by_its_token_is_reopened_under_a_new_one(app: FastAPI) -> None:
    service = ActionService(Upstream(FIRST, None), Upstream(SECOND))
    cookie = await login(app)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie)
        assert [await browser.next_chunk(), await browser.next_chunk()] == [FIRST, SECOND]
        browser.disconnect()
        await browser.ended()
    assert service.authorizations == ["Bearer test-token-1", "Bearer test-token-2"]
    assert all(upstream.closed for upstream in service.upstreams)


async def test_an_upstream_ending_before_its_first_frame_is_not_reopened(app: FastAPI) -> None:
    service = ActionService(Upstream(FIRST, None), Upstream(None))
    cookie = await login(app)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie)
        assert await browser.next_chunk() == FIRST
        await browser.ended()
    assert len(service.authorizations) == 2
    assert all(upstream.closed for upstream in service.upstreams)


if __name__ == "__main__":
    pytest_bazel.main()

"""The generated Action facade watch behind the real session middleware, over a scripted upstream.

The browser's side is driven straight through the ASGI app, because httpx's own ASGI transport would
buffer a body that never ends; logins and logouts go through httpx. Another replica is a second app
whose operator sessions are on a connection pool of their own, and whose listener never starts.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from sqlalchemy.engine import make_url

from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import app_services, create_app
from agentplane.app.database_updates import Channel, create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.live import LiveIndex
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.oidc import INSECURE_COOKIE
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.operator_streams import APP_URL, OIDC, EventSource, Upstream, login, logout, with_test_login
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.testing.thread_test_support import Replica
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.app.upstream_proxy import UpstreamProxy
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.postgres.wakeups import WakeupListener

ACTIONS_WATCH = "/upstream/actions/v1/action-requests:watch"
FIRST = b'{"result":{"snapshot":{"actionRequests":[]}}}\n'
SECOND = b'{"result":{"snapshotComplete":{}}}\n'


class StreamActions(FederatedOperatorActions):
    def __init__(self, tokens: Tokens) -> None:
        self.tokens = tokens

    async def token_for_request(self, request: Request) -> str:
        return await self.tokens()


class Tokens:
    """A new token per exchange, as the federation mints one."""

    def __init__(self) -> None:
        self.issued = 0

    async def __call__(self) -> str:
        self.issued += 1
        return f"test-token-{self.issued}"


class ActionService:
    """Answers each stream request with the next of `upstreams`, recording the bearer it came with."""

    def __init__(self, *upstreams: Upstream, status_code: int = 200) -> None:
        self.upstreams = upstreams
        self.status_code = status_code
        self.authorizations: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def answer(request: httpx.Request) -> httpx.Response:
            self.authorizations.append(request.headers["Authorization"])
            return httpx.Response(
                self.status_code,
                stream=self.upstreams[len(self.authorizations) - 1],
                headers={"content-type": "application/x-ndjson"},
            )

        return httpx.MockTransport(answer)


class RecordingActionService:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def answer(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"accepted": True})

        return httpx.MockTransport(answer)


@asynccontextmanager
async def operator_actions(app: FastAPI, transport: httpx.AsyncBaseTransport) -> AsyncIterator[None]:
    """`app`'s generated Actions facade proxy, over `transport`, for as long as the block runs."""
    async with httpx.AsyncClient(base_url="https://test-actions.invalid", transport=transport) as http:
        app_services(app).operator_actions = StreamActions(Tokens())
        app_services(app).actions_http = UpstreamProxy(http, token_file=None)
        yield


# The app `create_app` builds, over one replica's operator sessions and listener, with a login that
# needs no identity provider.
ServeApp = Callable[[OperatorSessionStore, WakeupListener], FastAPI]


@pytest.fixture
def serve(
    inventory: SandboxAccess,
    bridge: RunnerBridge,
    store: ThreadStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    electric_proxy: ElectricProxy,
    notifications: NotificationsOperatorClient,
    dispatcher_http: UpstreamProxy,
) -> ServeApp:
    def serving(sessions: OperatorSessionStore, updates: WakeupListener) -> FastAPI:
        app = create_app(
            inventory,
            bridge,
            store,
            TEST_MODELS,
            egress,
            decisions,
            live_index,
            action_policy,
            OIDC,
            event_logs=event_logs,
            content=content,
            database_updates=updates,
            operator_sessions=sessions,
            electric=electric_proxy,
            notifications=notifications,
            dispatcher_http=dispatcher_http,
            system_status=answering_system_status(),
        )

        return with_test_login(app)

    return serving


@pytest.fixture
def app(serve: ServeApp, operator_sessions: OperatorSessionStore, database_updates: WakeupListener) -> FastAPI:
    return serve(operator_sessions, database_updates)


@pytest.fixture
def other_replica(serve: ServeApp, replica: Replica, db_url: str) -> FastAPI:
    return serve(replica.operator_sessions, create_database_updates(make_url(db_url)))


async def test_upstream_status_before_downstream_headers(app: FastAPI) -> None:
    service = ActionService(Upstream(None), status_code=403)
    cookie = await login(app)
    async with operator_actions(app, service.transport()):
        messages = await EventSource(app, cookie, ACTIONS_WATCH).ended()
    assert messages[0]["status"] == 403
    assert service.upstreams[0].closed


async def test_connect_error_before_downstream_headers(app: FastAPI) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test connection failed", request=request)

    cookie = await login(app)
    async with operator_actions(app, httpx.MockTransport(fail)):
        messages = await EventSource(app, cookie, ACTIONS_WATCH).ended()
    assert messages[0]["status"] == 503


async def test_facade_forwards_session_token_path_body_and_bounded_user_agent(app: FastAPI) -> None:
    service = RecordingActionService()
    cookie = await login(app)
    user_agent = "test-browser/" + "x" * 400
    body = {"displayName": "test connection"}
    async with (
        operator_actions(app, service.transport()),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
        ) as browser,
    ):
        response = await browser.patch(
            "/upstream/actions/v1/operator/connections/test-connection?filter=test",
            json=body,
            headers={
                "Authorization": "Bearer browser-secret",
                "Origin": APP_URL,
                "User-Agent": user_agent,
                "X-Private-Test-Header": "test-private-header",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    request = service.requests[0]
    assert request.method == "PATCH"
    assert request.url.path == "/v1/operator/connections/test-connection"
    assert request.url.query == b"filter=test"
    assert request.headers["authorization"] == "Bearer test-token-1"
    assert request.headers["user-agent"] == user_agent[:300]
    assert request.headers["content-type"] == "application/json"
    assert request.headers.get("x-private-test-header") is None
    assert request.headers.get("cookie") is None
    assert request.content == b'{"displayName":"test connection"}'


async def test_public_callback_proxy_omits_browser_credentials() -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(
        base_url="https://test-actions.invalid", transport=httpx.MockTransport(answer)
    ) as client:
        proxy = UpstreamProxy(client, token_file=None)
        response = await proxy.open_request(
            "GET",
            "v1/mcp-linkage/callback",
            "state=single-use&code=authorization-code",
            bearer=None,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer browser-secret",
                "Cookie": "operator_session=browser-secret",
            },
        )
        await response.aclose()

    request = requests[0]
    assert request.url.path == "/v1/mcp-linkage/callback"
    assert request.url.query == b"state=single-use&code=authorization-code"
    assert request.headers["accept"] == "application/json"
    assert request.headers.get("authorization") is None
    assert request.headers.get("cookie") is None


async def test_mcp_callback_is_public_bridges_query_and_redirects_without_decoding_body(app: FastAPI) -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="not the browser contract")

    async with (
        operator_actions(app, httpx.MockTransport(answer)),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_URL, follow_redirects=False) as browser,
    ):
        response = await browser.get(
            "/mcp-linkage/callback?state=single-use&code=authorization-code",
            headers={"Authorization": "Bearer browser-secret", "Cookie": "operator_session=browser-secret"},
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/#/mcp-servers?linked=1"
    request = requests[0]
    assert request.url.path == "/v1/mcp-linkage/callback"
    assert request.url.query == b"state=single-use&code=authorization-code"
    assert request.headers["accept"] == "application/json"
    assert request.headers.get("authorization") is None
    assert request.headers.get("cookie") is None


async def test_mcp_callback_preserves_action_service_failure_status(app: FastAPI) -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(403, json={"code": 7, "message": "private provider detail"})

    async with (
        operator_actions(app, httpx.MockTransport(answer)),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_URL) as browser,
    ):
        response = await browser.get("/mcp-linkage/callback?state=single-use&code=bad-code")

    assert response.status_code == 403
    assert response.json() == {"detail": "MCP linkage callback failed"}
    assert len(requests) == 1


async def test_facade_refuses_non_versioned_paths_before_opening_upstream(app: FastAPI) -> None:
    service = RecordingActionService()
    cookie = await login(app)
    async with (
        operator_actions(app, service.transport()),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
        ) as browser,
    ):
        response = await browser.get("/upstream/actions/v2/action-requests")
    assert response.status_code == 404
    assert service.requests == []


async def test_facade_rejects_upstream_redirects_without_following_or_forwarding_location(app: FastAPI) -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://redirect.invalid/private"})

    cookie = await login(app)
    async with (
        operator_actions(app, httpx.MockTransport(redirect)),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=APP_URL,
            cookies={INSECURE_COOKIE: cookie},
            follow_redirects=False,
        ) as browser,
    ):
        response = await browser.get("/upstream/actions/v1/action-requests")
    assert response.status_code == 502
    assert response.headers.get("location") is None
    assert len(requests) == 1


@pytest.mark.parametrize("ending", ["reset", "disconnect", "drain"])
async def test_stream_cleanup(app: FastAPI, ending: str, caplog: pytest.LogCaptureFixture) -> None:
    upstream = Upstream(FIRST, httpx.ReadError("test upstream reset")) if ending == "reset" else Upstream(FIRST)
    service = ActionService(upstream)
    cookie = await login(app)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie, ACTIONS_WATCH)
        assert await browser.next_chunk() == FIRST
        if ending == "disconnect":
            browser.disconnect()
        elif ending == "drain":
            app_services(app).drain.begin()
        messages = await browser.ended()
    assert messages[0]["status"] == 200
    assert upstream.closed
    assert ("Action facade stream interrupted" in caplog.text) == (ending == "reset")


async def test_a_logout_on_another_replica_ends_the_stream(app: FastAPI, other_replica: FastAPI) -> None:
    service = ActionService(Upstream(FIRST))
    cookie = await login(app)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie, ACTIONS_WATCH)
        assert await browser.next_chunk() == FIRST
        await logout(other_replica, cookie)
        await browser.ended()
    assert service.upstreams[0].closed


async def test_another_sessions_logout_leaves_the_stream_open(
    app: FastAPI, other_replica: FastAPI, database_updates: WakeupListener
) -> None:
    upstream = Upstream(FIRST)
    service = ActionService(upstream)
    cookie, other = await login(app), await login(app)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie, ACTIONS_WATCH)
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
        browser = EventSource(app, cookie, ACTIONS_WATCH)
        assert await browser.next_chunk() == FIRST
        await browser.ended()
    assert service.upstreams[0].closed


async def test_a_clean_upstream_eof_is_relayed_for_the_browser_to_reconnect(app: FastAPI) -> None:
    service = ActionService(Upstream(FIRST, None), Upstream(None))
    cookie = await login(app)
    async with operator_actions(app, service.transport()):
        browser = EventSource(app, cookie, ACTIONS_WATCH)
        assert await browser.next_chunk() == FIRST
        await browser.ended()
    assert len(service.authorizations) == 1
    assert service.upstreams[0].closed


if __name__ == "__main__":
    pytest_bazel.main()

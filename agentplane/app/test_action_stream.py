"""The generated Action facade watch behind the real session middleware, over a scripted upstream.

The browser's side is driven straight through the ASGI app, because httpx's own ASGI transport would
buffer a body that never ends; logins and logouts go through httpx. Another replica is a second app
whose operator sessions are on a connection pool of their own, and whose listener never starts.
"""

# Generated protobuf annotations need the protobuf typing dependency.
# gazelle:include_dep @pypi//protobuf
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import grpc
import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from sqlalchemy.engine import make_url

from agentplane.action_service import action_service_pb2, action_service_pb2_grpc
from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import app_services, create_app
from agentplane.app.database_updates import Channel, create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.grpc_stream import OperatorGrpcClient
from agentplane.app.live import LiveIndex
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.oidc import INSECURE_COOKIE
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.fake_action_service import FakeActionService
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.operator_streams import APP_URL, OIDC, EventSource, login, logout, with_test_login
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.testing.thread_test_support import Replica
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.postgres.wakeups import WakeupListener

ACTIONS_WATCH = "/upstream/actions/v1/action-requests:watch"
FIRST = b'{"result":{"snapshotComplete":{}}}\n'


async def test_cancelling_watch_handshake_cancels_the_grpc_call(
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
) -> None:
    service, stub, client = action_service
    service.watch_gate = asyncio.Event()
    opening = asyncio.create_task(
        client.open_watch(
            stub.WatchActionRequests, action_service_pb2.WatchActionRequestsRequest(), bearer_token="test-watch-token"
        )
    )

    async with asyncio.timeout(10):
        await service.watch_started.wait()
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        await service.watch_cancelled.wait()


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


@asynccontextmanager
async def operator_actions(app: FastAPI, transport: httpx.AsyncBaseTransport) -> AsyncIterator[None]:
    """The public MCP OAuth callback's narrow HTTP backend, over `transport`."""
    async with httpx.AsyncClient(base_url="https://test-actions.invalid", transport=transport) as http:
        app_services(app).operator_actions = StreamActions(Tokens())
        app_services(app).mcp_callback_http = http
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
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
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
            operator_actions=StreamActions(Tokens()),
            event_logs=event_logs,
            content=content,
            database_updates=updates,
            operator_sessions=sessions,
            electric=electric_proxy,
            notifications=notifications,
            action_service=action_service[1],
            operator_grpc=action_service[2],
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


@pytest.mark.parametrize(
    ("grpc_code", "http_status"), [(grpc.StatusCode.PERMISSION_DENIED, 403), (grpc.StatusCode.UNAVAILABLE, 503)]
)
async def test_grpc_status_before_downstream_headers(
    app: FastAPI,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
    grpc_code: grpc.StatusCode,
    http_status: int,
) -> None:
    service, _, _ = action_service
    service.watch_error_before_first = grpc_code
    cookie = await login(app)
    messages = await EventSource(app, cookie, ACTIONS_WATCH).ended()
    assert messages[0]["status"] == http_status
    assert service.calls[0].method == "WatchActionRequests"
    assert service.calls[0].bearer_token == "Bearer test-token-1"


async def test_unconfigured_action_service_before_downstream_headers(app: FastAPI) -> None:
    cookie = await login(app)
    app_services(app).action_service = None
    messages = await EventSource(app, cookie, ACTIONS_WATCH).ended()
    assert messages[0]["status"] == 503


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


async def test_unknown_actions_version_is_not_routed(app: FastAPI) -> None:
    cookie = await login(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
    ) as browser:
        # The Actions path is an explicit BFF contract, not a versioned-path catch-all.
        response = await browser.get("/upstream/actions/v2/action-requests")
    assert response.status_code == 404


@pytest.mark.parametrize("ending", ["rpc-error", "disconnect", "drain"])
async def test_stream_cleanup(
    app: FastAPI,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
    ending: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _, _ = action_service
    cookie = await login(app)
    browser = EventSource(app, cookie, ACTIONS_WATCH)
    assert await browser.next_chunk() == FIRST
    if ending == "rpc-error":
        service.watch_frames.put_nowait(grpc.StatusCode.UNAVAILABLE)
    elif ending == "disconnect":
        browser.disconnect()
    elif ending == "drain":
        app_services(app).drain.begin()
    messages = await browser.ended()
    assert messages[0]["status"] == 200
    assert ("Action Service stream interrupted" in caplog.text) == (ending == "rpc-error")
    if ending != "rpc-error":
        async with asyncio.timeout(10):
            await service.watch_cancelled.wait()


async def test_a_logout_on_another_replica_ends_the_stream(
    app: FastAPI,
    other_replica: FastAPI,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
) -> None:
    service, _, _ = action_service
    cookie = await login(app)
    browser = EventSource(app, cookie, ACTIONS_WATCH)
    assert await browser.next_chunk() == FIRST
    await logout(other_replica, cookie)
    await browser.ended()
    async with asyncio.timeout(10):
        await service.watch_cancelled.wait()


async def test_another_sessions_logout_leaves_the_stream_open(
    app: FastAPI,
    other_replica: FastAPI,
    database_updates: WakeupListener,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
) -> None:
    service, _, _ = action_service
    cookie, other = await login(app), await login(app)
    browser = EventSource(app, cookie, ACTIONS_WATCH)
    assert await browser.next_chunk() == FIRST
    heard = asyncio.Event()
    with database_updates.changes[Channel.OPERATOR_SESSIONS].subscribe(heard):
        await logout(other_replica, other)
        await asyncio.wait_for(heard.wait(), timeout=10)
    # The stream's own reader woke on that same notification; an Action update still arrives.
    service.watch_frames.put_nowait(
        action_service_pb2.WatchActionRequestsResponse(
            upsert=action_service_pb2.ActionRequest(id="request-after-other-logout")
        )
    )
    assert b"request-after-other-logout" in await browser.next_chunk()
    await logout(other_replica, cookie)
    await browser.ended()
    async with asyncio.timeout(10):
        await service.watch_cancelled.wait()


async def test_the_stream_ends_when_its_session_expires(
    app: FastAPI,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
) -> None:
    service, _, _ = action_service
    # Long enough to open the stream inside it; nothing but the expiry ends this one.
    cookie = await login(app, seconds=2)
    browser = EventSource(app, cookie, ACTIONS_WATCH)
    assert await browser.next_chunk() == FIRST
    await browser.ended()
    async with asyncio.timeout(10):
        await service.watch_cancelled.wait()


async def test_a_clean_grpc_eof_ends_the_watch_for_the_browser_to_reconnect(
    app: FastAPI,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
) -> None:
    service, _, _ = action_service
    service.watch_frames.put_nowait(None)
    cookie = await login(app)
    browser = EventSource(app, cookie, ACTIONS_WATCH)
    assert await browser.next_chunk() == FIRST
    await browser.ended()
    assert [call.method for call in service.calls] == ["WatchActionRequests"]
    assert [call.bearer_token for call in service.calls] == ["Bearer test-token-1"]


if __name__ == "__main__":
    pytest_bazel.main()

"""Typed browser routes over the app's native Connection and Session Dispatcher gRPC clients."""

from __future__ import annotations

# Generated protobuf annotations need the protobuf typing dependency.
# gazelle:include_dep @pypi//protobuf
import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override

import grpc
import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from sqlalchemy import update

from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import app_services, create_app
from agentplane.app.database_updates import Channel
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.grpc_stream import OperatorGrpcClient
from agentplane.app.live import LiveIndex
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.oidc import INSECURE_COOKIE
from agentplane.app.operator_sessions import BrowserSession, OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.dispatcher import served_dispatcher
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.operator_streams import APP_URL, OIDC, EventSource, login, logout, with_test_login
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc.calls import dial
from agentplane.grpc.serving import serving
from agentplane.mcp_edge import connection_service_pb2, connection_service_pb2_grpc
from agentplane.postgres.wakeups import WakeupListener, notify
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2, session_dispatcher_pb2_grpc

CONNECTIONS_WATCH = "/upstream/connections/v1/operator/connections:watch"
DISPATCHES_WATCH = "/upstream/dispatcher/v1/dispatches:watch"
CONNECTION_FRAME = b'{"result":{"snapshotComplete":{}}}\n'
DISPATCH_FRAME = b'{"result":{"caughtUp":{}}}\n'


class FakeFederation(FederatedOperatorActions):
    """A valid federation provider with a fixed test bearer."""

    def __init__(self) -> None:
        self.calls = 0

    async def token_for_request(self, request: Request) -> str:
        self.calls += 1
        return f"test-operator-token-{self.calls}"


class FakeConnectionService(connection_service_pb2_grpc.ConnectionServiceServicer):
    def __init__(self) -> None:
        self.authorization: list[str | None] = []
        self.updated: list[connection_service_pb2.UpdateConnectionRequest] = []
        self.watch_cancelled = asyncio.Event()
        self.reject_watch = False

    @override
    async def ListConnections(
        self, request: connection_service_pb2.ListConnectionsRequest, context: grpc.aio.ServicerContext
    ) -> connection_service_pb2.ListConnectionsResponse:
        self.authorization.append(_authorization(context))
        return connection_service_pb2.ListConnectionsResponse()

    @override
    async def UpdateConnection(
        self, request: connection_service_pb2.UpdateConnectionRequest, context: grpc.aio.ServicerContext
    ) -> connection_service_pb2.UpdateConnectionResponse:
        self.authorization.append(_authorization(context))
        self.updated.append(request)
        return connection_service_pb2.UpdateConnectionResponse(
            connection=connection_service_pb2.Connection(
                id=request.connection_id, display_name=request.display_name, version=request.expected_version
            )
        )

    @override
    async def WatchConnections(
        self, request: connection_service_pb2.WatchConnectionsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[connection_service_pb2.WatchConnectionsResponse]:
        self.authorization.append(_authorization(context))
        if self.reject_watch:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "test refusal")
        try:
            yield connection_service_pb2.WatchConnectionsResponse(snapshot_complete={})
            await asyncio.Event().wait()
        finally:
            self.watch_cancelled.set()


class FakeDispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    def __init__(self) -> None:
        self.authorization: list[str | None] = []
        self.list_requests: list[session_dispatcher_pb2.ListDispatchesRequest] = []
        self.watch_requests: list[session_dispatcher_pb2.WatchDispatchesRequest] = []
        self.watch_cancelled = asyncio.Event()
        self.reject_watch = False

    @override
    async def ListDispatches(
        self, request: session_dispatcher_pb2.ListDispatchesRequest, context: grpc.aio.ServicerContext
    ) -> session_dispatcher_pb2.ListDispatchesResponse:
        self.authorization.append(_authorization(context))
        self.list_requests.append(request)
        return session_dispatcher_pb2.ListDispatchesResponse()

    @override
    async def WatchDispatches(
        self, request: session_dispatcher_pb2.WatchDispatchesRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[session_dispatcher_pb2.WatchDispatchesResponse]:
        self.authorization.append(_authorization(context))
        self.watch_requests.append(request)
        if self.reject_watch:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "test refusal")
        try:
            yield session_dispatcher_pb2.WatchDispatchesResponse(caught_up={})
            await asyncio.Event().wait()
        finally:
            self.watch_cancelled.set()


def _authorization(context: grpc.aio.ServicerContext) -> str | None:
    return next((str(value) for key, value in context.invocation_metadata() or () if key == "authorization"), None)


@pytest.fixture
async def connection_peer(
    tmp_path: Path,
) -> AsyncIterator[tuple[connection_service_pb2_grpc.ConnectionServiceStub, FakeConnectionService, OperatorGrpcClient]]:
    service = FakeConnectionService()
    server = grpc.aio.server()
    connection_service_pb2_grpc.add_ConnectionServiceServicer_to_server(service, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        yield (
            connection_service_pb2_grpc.ConnectionServiceStub(channel),
            service,
            OperatorGrpcClient(request_timeout_s=2),
        )


@pytest.fixture
def app(
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
    operator_sessions: OperatorSessionStore,
    database_updates: WakeupListener,
    connection_peer: tuple[
        connection_service_pb2_grpc.ConnectionServiceStub, FakeConnectionService, OperatorGrpcClient
    ],
) -> FastAPI:
    connection, _, operator_grpc = connection_peer
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
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        electric=electric_proxy,
        connection_service=connection,
        operator_grpc=operator_grpc,
        notifications=notifications,
        system_status=answering_system_status(),
    )
    app_services(app).operator_actions = FakeFederation()
    return with_test_login(app)


async def test_connection_routes_use_federated_bearer_typed_requests_and_protojson(
    app: FastAPI,
    connection_peer: tuple[
        connection_service_pb2_grpc.ConnectionServiceStub, FakeConnectionService, OperatorGrpcClient
    ],
) -> None:
    _, peer, _ = connection_peer
    cookie = await login(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
    ) as browser:
        listed = await browser.get("/upstream/connections/v1/operator/connections")
        updated = await browser.patch(
            "/upstream/connections/v1/operator/connections/connection-1",
            json={"expectedVersion": "7", "displayName": "renamed"},
            headers={"Origin": APP_URL},
        )
    assert listed.status_code == 200
    assert listed.json() == {"connections": []}
    assert updated.status_code == 200
    connection = updated.json()["connection"]
    assert (connection["displayName"], connection["id"], connection["version"]) == ("renamed", "connection-1", "7")
    assert peer.authorization == ["Bearer test-operator-token-1", "Bearer test-operator-token-2"]
    assert len(peer.updated) == 1
    assert (peer.updated[0].connection_id, peer.updated[0].expected_version, peer.updated[0].display_name) == (
        "connection-1",
        7,
        "renamed",
    )


async def test_connection_watch_uses_ndjson_and_fails_auth_before_headers(
    app: FastAPI,
    connection_peer: tuple[
        connection_service_pb2_grpc.ConnectionServiceStub, FakeConnectionService, OperatorGrpcClient
    ],
) -> None:
    _, peer, _ = connection_peer
    cookie = await login(app)
    browser = EventSource(app, cookie, CONNECTIONS_WATCH)
    assert await browser.next_chunk() == CONNECTION_FRAME
    await logout(app, cookie)
    await browser.ended()
    async with asyncio.timeout(5):
        await peer.watch_cancelled.wait()
    assert peer.authorization == ["Bearer test-operator-token-1"]

    peer.reject_watch = True
    refused = await EventSource(app, await login(app), CONNECTIONS_WATCH).ended()
    assert refused[0]["status"] == 401


async def test_connection_path_field_mismatch_is_rejected_before_rpc(app: FastAPI) -> None:
    cookie = await login(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
    ) as browser:
        response = await browser.patch(
            "/upstream/connections/v1/operator/connections/path-id",
            json={"connectionId": "body-id", "expectedVersion": "1", "displayName": "x"},
            headers={"Origin": APP_URL},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("ending", ["logout", "expiry", "disconnect", "drain"])
async def test_dispatcher_watch_cancellation_tracks_operator_session_and_response(
    app: FastAPI, bridge: RunnerBridge, operator_sessions: OperatorSessionStore, tmp_path: Path, ending: str
) -> None:
    peer = FakeDispatcher()
    token_file = tmp_path / "dispatcher-token"
    async with served_dispatcher(peer, token_file, request_timeout_s=2) as client:
        bridge.dispatcher = client
        cookie = await login(app)
        browser = EventSource(app, cookie, DISPATCHES_WATCH)
        assert await browser.next_chunk() == DISPATCH_FRAME
        if ending == "expiry":
            # Expire only after the stream is open; a subsecond cookie age would log out at login.
            async with operator_sessions.sessions.begin() as db:
                await db.execute(update(BrowserSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
                await notify(db, Channel.OPERATOR_SESSIONS)
        elif ending == "logout":
            await logout(app, cookie)
        elif ending == "disconnect":
            browser.disconnect()
        elif ending == "drain":
            app_services(app).drain.begin()
        await browser.ended()
        async with asyncio.timeout(5):
            await peer.watch_cancelled.wait()


async def test_dispatcher_routes_preserve_filters_and_read_the_rotating_service_token(
    app: FastAPI, bridge: RunnerBridge, tmp_path: Path
) -> None:
    peer = FakeDispatcher()
    token_file = tmp_path / "dispatcher-token"
    async with served_dispatcher(peer, token_file, request_timeout_s=2) as client:
        bridge.dispatcher = client
        cookie = await login(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
        ) as browser:
            response = await browser.get(
                "/upstream/dispatcher/v1/dispatches",
                params=[
                    ("filter.sessionId", "session-1"),
                    ("filter.commandIds", "command-1"),
                    ("filter.commandIds", "command-2"),
                ],
            )
        assert response.status_code == 200
        assert response.json() == {"dispatches": []}
        assert peer.authorization == ["Bearer test-app-command-peer"]
        assert peer.list_requests[0].filter.session_id == "session-1"
        assert list(peer.list_requests[0].filter.command_ids) == ["command-1", "command-2"]

        token_file.write_text("rotated-dispatcher-token\n")
        stream = EventSource(
            app,
            cookie,
            DISPATCHES_WATCH,
            b"filter.sessionId=session-1&filter.commandIds=command-1&filter.commandIds=command-2",
        )
        assert await stream.next_chunk() == DISPATCH_FRAME
        stream.disconnect()
        await stream.ended()
        async with asyncio.timeout(5):
            await peer.watch_cancelled.wait()
        assert peer.authorization == ["Bearer test-app-command-peer", "Bearer rotated-dispatcher-token"]
        assert peer.watch_requests[0].filter.session_id == "session-1"
        assert list(peer.watch_requests[0].filter.command_ids) == ["command-1", "command-2"]


async def test_dispatcher_watch_authentication_failure_is_an_http_error_before_headers(
    app: FastAPI, bridge: RunnerBridge, tmp_path: Path
) -> None:
    peer = FakeDispatcher()
    peer.reject_watch = True
    async with served_dispatcher(peer, tmp_path / "dispatcher-token", request_timeout_s=2) as client:
        bridge.dispatcher = client
        refused = await EventSource(app, await login(app), DISPATCHES_WATCH).ended()
    assert refused[0]["status"] == 401


async def test_unmapped_facade_paths_are_not_forwarded(app: FastAPI) -> None:
    cookie = await login(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
    ) as browser:
        response = await browser.get("/upstream/connections/v1/operator/not-an-rpc")
    assert response.status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()

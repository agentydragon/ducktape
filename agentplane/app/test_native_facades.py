"""Real Go HTTP -> generated Dispatcher gateway, with operator-session cancellation."""

import asyncio
import json
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import override

import grpc
import httpx
import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.testing.replication_process import AppProcess, GatewayAccess, SandboxesAccess, app_process
from agentplane.environment_runtime_transport.config import SESSION_DISPATCHER_URI, ClientTLS
from agentplane.history_service.client import HistoryServiceClient
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.tls import issue_workload_leaf, make_ca

# gazelle:include_dep //agentplane/app:integration_fixtures
pytest_plugins = ("agentplane.app.integration_fixtures",)

WATCH = "/upstream/dispatcher/v1/dispatches:watch"
# gazelle:include_dep @pypi//protobuf


class Dispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    def __init__(self) -> None:
        self.authorization: list[str] = []
        self.requests: list[session_dispatcher_pb2.ListDispatchesRequest] = []
        self.watches: list[session_dispatcher_pb2.WatchDispatchesRequest] = []
        self.cancelled = asyncio.Event()
        self.reject_watch = False
        self.clean_eof = False

    def record_authorization(self, context: grpc.aio.ServicerContext) -> None:
        self.authorization.extend(
            str(value) for key, value in (context.invocation_metadata() or ()) if key == "authorization"
        )

    @override
    async def ListDispatches(self, request, context):
        self.record_authorization(context)
        self.requests.append(request)
        return session_dispatcher_pb2.ListDispatchesResponse()

    @override
    async def WatchDispatches(self, request, context):
        self.record_authorization(context)
        self.watches.append(request)
        if self.reject_watch:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "test refusal")
        await context.send_initial_metadata((("x-backend-private", "must-not-reach-browser"),))
        try:
            yield session_dispatcher_pb2.WatchDispatchesResponse(caught_up={})
            if not self.clean_eof:
                await asyncio.Event().wait()
        finally:
            self.cancelled.set()


@dataclass
class Peer:
    service: Dispatcher
    target: str
    token: Path
    tls: ClientTLS


@pytest.fixture
async def peer(tmp_path: Path) -> AsyncIterator[Peer]:
    service = Dispatcher()
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(service, server)
    token = tmp_path / "dispatcher-token"
    token.write_text("test-dispatcher-token")
    ca = make_ca("Dispatcher facade test")
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca.cert_pem)
    certificate, key = issue_workload_leaf(ca, SESSION_DISPATCHER_URI, tmp_path)
    port = server.add_secure_port(
        "127.0.0.1:0", grpc.ssl_server_credentials([(key.read_bytes(), certificate.read_bytes())])
    )
    await server.start()
    try:
        yield Peer(service, f"127.0.0.1:{port}", token, ClientTLS(ca_file=ca_file, server_uri=SESSION_DISPATCHER_URI))
    finally:
        await server.stop(grace=0)


@pytest.fixture
def app_config(peer: Peer) -> dict[str, object]:
    return {
        "session_dispatcher_target": peer.target,
        "session_dispatcher_token_file": str(peer.token),
        "session_dispatcher_tls": peer.tls.model_dump(mode="json"),
    }


async def test_dispatcher_preserves_filters_reads_rotating_token_and_filters_headers(
    http: httpx.AsyncClient, peer: Peer
) -> None:
    params = httpx.QueryParams("filter.sessionId=session-1&filter.commandIds=one&filter.commandIds=two")
    listed = await http.get("/upstream/dispatcher/v1/dispatches", params=params)
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"dispatches": []}
    assert peer.service.requests[0].filter.session_id == "session-1"
    assert list(peer.service.requests[0].filter.command_ids) == ["one", "two"]
    peer.token.write_text("rotated-dispatcher-token\n")
    async with http.stream("GET", WATCH, params=params) as response:
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        assert not any(key.startswith("grpc-metadata-") for key in response.headers)
        lines = response.aiter_lines()
        assert json.loads(await anext(lines)) == {"result": {"caughtUp": {}}}
    async with asyncio.timeout(5):
        await peer.service.cancelled.wait()
    assert peer.service.authorization == ["Bearer test-dispatcher-token", "Bearer rotated-dispatcher-token"]
    assert peer.service.watches[0].filter == peer.service.requests[0].filter


@pytest.mark.parametrize("ending", ["logout", "expiry", "disconnect", "sigterm"])
async def test_dispatcher_stream_cancels_upstream_and_closes_cleanly(
    app: AppProcess, http: httpx.AsyncClient, engine: AsyncEngine, peer: Peer, ending: str
) -> None:
    async with http.stream("GET", WATCH) as response:
        assert response.status_code == 200
        lines = response.aiter_lines()
        assert "caughtUp" in await anext(lines)
        if ending == "logout":
            assert (await http.post("/auth/logout")).status_code == 303
        elif ending == "expiry":
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE operator_browser_session SET expires_at = now() - interval '1 second'")
                )
                await connection.execute(text("SELECT pg_notify('agentplane_operator_sessions', '')"))
        elif ending == "sigterm":
            app.process.send_signal(signal.SIGTERM)
        if ending != "disconnect":
            async with asyncio.timeout(5):
                assert all([json.loads(line)["error"]["code"] == 1 async for line in lines])
    async with asyncio.timeout(5):
        await peer.service.cancelled.wait()
    if ending == "sigterm":
        async with asyncio.timeout(10):
            assert await app.process.wait() == 0, app.stderr.read_text()
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
                    )
                )
                == 0
            )


async def test_dispatcher_watch_refusal_precedes_response_headers(http: httpx.AsyncClient, peer: Peer) -> None:
    peer.service.reject_watch = True
    response = await http.get(WATCH)
    assert response.status_code == 401, response.text


async def test_clean_grpc_eof_ends_browser_watch(http: httpx.AsyncClient, peer: Peer) -> None:
    peer.service.clean_eof = True
    response = await http.get(WATCH)
    assert response.status_code == 200
    assert [json.loads(line) for line in response.text.splitlines()] == [{"result": {"caughtUp": {}}}]


async def test_another_replica_logout_ends_only_its_sessions_stream(
    app: AppProcess,
    http: httpx.AsyncClient,
    peer: Peer,
    db_url: str,
    history_client: HistoryServiceClient,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    app_token_file: Path,
    app_config: dict[str, object],
) -> None:
    async with (
        app_process(
            db_url,
            history_client,
            sandboxes=SandboxesAccess(sandbox.server.kubeconfig, sandbox.namespace),
            gateway=GatewayAccess(gateway_service.target, app_token_file, gateway_service.client_tls),
            config=app_config,
        ) as replica,
        httpx.AsyncClient(base_url=replica.url, headers={"Origin": replica.url}) as other,
    ):
        async with http.stream("GET", WATCH) as response:
            lines = response.aiter_lines()
            assert "caughtUp" in await anext(lines)
            unrelated = await other.post("/auth/logout", cookies=replica.cookies)
            assert unrelated.status_code == 303
            assert not peer.service.cancelled.is_set()
            # A request through this stream's still-live session continues to work.
            assert (await http.get("/auth/me")).status_code == 200
            ended = await other.post("/auth/logout", cookies=app.cookies)
            assert ended.status_code == 303
            async with asyncio.timeout(5):
                assert all([json.loads(line)["error"]["code"] == 1 async for line in lines])
        async with asyncio.timeout(5):
            await peer.service.cancelled.wait()


async def test_unknown_facade_paths_are_not_forwarded(http: httpx.AsyncClient, peer: Peer) -> None:
    assert (await http.get("/upstream/dispatcher/v2/dispatches")).status_code == 404
    assert (await http.get("/upstream/dispatcher/v1/not-a-route")).status_code == 404
    assert not peer.service.requests


if __name__ == "__main__":
    pytest_bazel.main()

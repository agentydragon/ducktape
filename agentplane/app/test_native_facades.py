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
from agentplane.harness_host import harness_host_pb2
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.net.peertls.config import SESSION_DISPATCHER_URI, ClientTLS
from agentplane.proto import session_pb2
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.testing.tls import issue_workload_leaf, make_ca

# gazelle:include_dep //agentplane/app:integration_fixtures
pytest_plugins = ("agentplane.app.integration_fixtures",)

WATCH = "/upstream/dispatcher/v1/dispatches:watch"
# gazelle:include_dep @pypi//protobuf


class Dispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    def __init__(self) -> None:
        self.authorization: list[str] = []
        self.requests: list[session_dispatcher_pb2.ListDispatchesRequest] = []
        self.receipt_pages: list[session_dispatcher_pb2.ListSessionDispatchesRequest] = []
        self.watches: list[session_dispatcher_pb2.WatchDispatchesRequest] = []
        self.cancelled = asyncio.Event()
        self.reject_watch: grpc.StatusCode | None = None
        self.clean_eof = False
        self.inventory_requests: list[session_dispatcher_pb2.WatchSessionsRequest] = []
        self.inventory_updates: asyncio.Queue[session_dispatcher_pb2.Session] = asyncio.Queue()
        self.inventory_cancelled = asyncio.Event()
        self.sessions: dict[str, session_dispatcher_pb2.Session] = {}

    @override
    async def GetSession(self, request, context):
        session = self.sessions.get(request.session_id)
        if session is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "Session not found")
        return session_dispatcher_pb2.GetSessionResponse(session=session)

    def record_authorization(self, context: grpc.aio.ServicerContext) -> None:
        self.authorization.extend(
            str(value) for key, value in (context.invocation_metadata() or ()) if key == "authorization"
        )

    @override
    async def WatchSessions(self, request, context):
        self.inventory_requests.append(request)
        session = session_dispatcher_pb2.Session(
            session_id="10000000-0000-4000-8000-000000000031",
            status=session_dispatcher_pb2.SessionStatus(
                materialization=session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_WAITING
            ),
        )
        session.created_at.FromSeconds(1_700_000_000)
        try:
            yield session_dispatcher_pb2.WatchSessionsResponse(session=session, resume_token="test-snapshot-token")
            yield session_dispatcher_pb2.WatchSessionsResponse(caught_up={}, resume_token="test-snapshot-token")
            while True:
                updated = await self.inventory_updates.get()
                yield session_dispatcher_pb2.WatchSessionsResponse(session=updated, resume_token="test-update-token")
        finally:
            self.inventory_cancelled.set()

    @override
    async def BatchGetDispatches(self, request, context):
        return session_dispatcher_pb2.BatchGetDispatchesResponse(
            results=[
                session_dispatcher_pb2.BatchGetDispatchesResponse.Result(command_id=key, not_found={})
                for key in request.command_ids
            ]
        )

    @override
    async def ListSessionDispatches(self, request, context):
        self.receipt_pages.append(request)
        return session_dispatcher_pb2.ListSessionDispatchesResponse(
            dispatches=[session_dispatcher_pb2.Dispatch(command_id="test-command", receipt_version=9007199254740993)],
            coverage=session_dispatcher_pb2.ListSessionDispatchesResponse.RETAINED_DISPATCH_ROWS,
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
        if self.reject_watch is not None:
            await context.abort(self.reject_watch, "test refusal")
        await context.send_initial_metadata((("x-backend-private", "must-not-reach-browser"),))
        try:
            yield session_dispatcher_pb2.WatchDispatchesResponse(caught_up={}, resume_token="test-dispatch-cursor")
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
    async with http.stream("GET", WATCH, params=params.add("resumeToken", "test-request-cursor")) as response:
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        assert not any(key.startswith("grpc-metadata-") for key in response.headers)
        lines = response.aiter_lines()
        assert json.loads(await anext(lines)) == {"result": {"caughtUp": {}, "resumeToken": "test-dispatch-cursor"}}
    async with asyncio.timeout(5):
        await peer.service.cancelled.wait()
    assert peer.service.authorization == ["Bearer test-dispatcher-token", "Bearer rotated-dispatcher-token"]
    assert peer.service.watches[0].filter == peer.service.requests[0].filter
    assert peer.service.watches[0].resume_token == "test-request-cursor"


async def test_session_receipt_page_preserves_scope_and_cursor(http: httpx.AsyncClient, peer: Peer) -> None:
    response = await http.get(
        "/upstream/dispatcher/v1/sessions/test-session/dispatches", params={"limit": 4, "pageToken": "test-page"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["coverage"] == "RETAINED_DISPATCH_ROWS"
    assert response.json()["dispatches"][0]["receiptVersion"] == "9007199254740993"
    assert peer.service.receipt_pages == [
        session_dispatcher_pb2.ListSessionDispatchesRequest(session_id="test-session", limit=4, page_token="test-page")
    ]
    batch = await http.get(
        "/upstream/dispatcher/v1/sessions/test-session/dispatches:batchGet",
        params=[("commandIds", "two"), ("commandIds", "one")],
    )
    assert batch.status_code == 200
    assert batch.json()["results"] == [{"commandId": key, "notFound": {}} for key in ("two", "one")]


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


@pytest.mark.parametrize(
    ("code", "http_status"), [(grpc.StatusCode.UNAUTHENTICATED, 401), (grpc.StatusCode.OUT_OF_RANGE, 400)]
)
async def test_dispatcher_watch_refusal_precedes_response_headers(
    http: httpx.AsyncClient, peer: Peer, code: grpc.StatusCode, http_status: int
) -> None:
    peer.service.reject_watch = code
    response = await http.get(WATCH)
    assert response.status_code == http_status, response.text
    assert response.json()["error"]["code"] == code.value[0]


async def test_clean_grpc_eof_ends_browser_watch(http: httpx.AsyncClient, peer: Peer) -> None:
    peer.service.clean_eof = True
    response = await http.get(WATCH)
    assert response.status_code == 200
    assert [json.loads(line) for line in response.text.splitlines()] == [
        {"result": {"caughtUp": {}, "resumeToken": "test-dispatch-cursor"}}
    ]


async def test_another_replica_logout_ends_only_its_sessions_stream(
    app: AppProcess,
    http: httpx.AsyncClient,
    peer: Peer,
    db_url: str,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    gateway_access: GatewayAccess,
    app_config: dict[str, object],
) -> None:
    async with (
        app_process(
            db_url,
            history_service,
            sandboxes=SandboxesAccess(sandbox.server.kubeconfig, sandbox.namespace),
            gateway=gateway_access,
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


async def test_session_watch_flushes_snapshot_and_updates_through_dispatcher_facade(
    http: httpx.AsyncClient, peer: Peer
) -> None:
    path = "/upstream/dispatcher/v1/sessions:watch"
    async with http.stream("GET", path, params={"resumeToken": "test-resume-token"}) as response:
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        lines = response.aiter_lines()
        async with asyncio.timeout(5):
            initial = json.loads(await anext(lines))["result"]
            caught_up = json.loads(await anext(lines))["result"]
        session_id = "10000000-0000-4000-8000-000000000031"
        assert initial["session"]["sessionId"] == session_id
        assert initial["resumeToken"] == "test-snapshot-token"
        assert "caughtUp" in caught_up
        assert peer.service.inventory_requests[0].resume_token == "test-resume-token"
        # The snapshot must reach the browser while the upstream is waiting for the next write.
        peer.service.inventory_updates.put_nowait(
            session_dispatcher_pb2.Session(
                session_id=session_id,
                status=session_dispatcher_pb2.SessionStatus(
                    materialization=session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_FAILED
                ),
            )
        )
        async with asyncio.timeout(5):
            updated = json.loads(await anext(lines))["result"]
        assert updated["session"]["status"]["materialization"] == "SESSION_MATERIALIZATION_FAILED"
        assert updated["resumeToken"] == "test-update-token"
        assert (await http.post("/auth/logout")).status_code == 303
        async with asyncio.timeout(5):
            assert all([json.loads(line)["error"]["code"] == 1 async for line in lines])
    async with asyncio.timeout(5):
        await peer.service.inventory_cancelled.wait()


@pytest.mark.parametrize("first", ["rename", "archive", "unarchive"])
async def test_metadata_first_write_without_history(http: httpx.AsyncClient, peer: Peer, first: str) -> None:
    session_id = "10000000-0000-4000-8000-000000000032"
    peer.service.sessions[session_id] = session_dispatcher_pb2.Session(
        session_id=session_id,
        status=session_dispatcher_pb2.SessionStatus(
            activity=session_dispatcher_pb2.SessionActivity(
                observed=harness_host_pb2.Session(
                    session_id=session_id,
                    environment_hosted=harness_host_pb2.EnvironmentHostedSession(
                        setup=session_pb2.SETUP_STATE_NOT_REQUIRED,
                        harness=harness_host_pb2.HarnessAttachment(
                            state=harness_host_pb2.HarnessAttachment.STATE_ATTACHED
                        ),
                    ),
                )
            )
        ),
    )
    path = f"/upstream/app/v1/threads/{session_id}"
    assert (await http.get(f"/upstream/history/v1/sessions/{session_id}/summary")).status_code == 404
    assert (await http.get(path)).status_code == 404
    if first == "rename":
        response = await http.patch(path, json={"name": "Test unrecorded Session"})
    else:
        response = await http.post(f"{path}/{first}")
    assert response.status_code == 200, response.text
    assert (await http.patch(path, json={"name": "Test unrecorded Session"})).status_code == 200
    archived = await http.post(f"{path}/archive")
    assert archived.status_code == 200, archived.text
    metadata = (await http.get(path)).json()
    assert metadata["name"] == "Test unrecorded Session"
    assert metadata["archived"]
    assert (await http.post(f"{path}/unarchive")).status_code == 200
    metadata = (await http.get(path)).json()
    assert metadata["name"] == "Test unrecorded Session"
    assert not metadata.get("archived", False)


if __name__ == "__main__":
    pytest_bazel.main()

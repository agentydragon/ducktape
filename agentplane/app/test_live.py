"""Live views through a real Go HTTP server, Kubernetes watches and PostgreSQL LISTEN."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import grpc
import httpx
import pytest
import pytest_bazel
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.testing.egress_proxy import FakeEgressAdmin, decision
from agentplane.app.testing.history import SessionHistory
from agentplane.app.testing.models import DecisionsSnapshot, DecisionsUnavailable, ThreadView, WatchHealth
from agentplane.app.testing.replication_process import AppProcess, SandboxesAccess, app_process
from agentplane.app.testing.thread_test_support import ThreadHTTP
from agentplane.egress import egress_admin_pb2_grpc
from agentplane.egress.decisions import Outcome
from agentplane.grpc.serving import serving
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.history import History
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer

# gazelle:include_dep @pypi//protobuf


class ThreadsSnapshot(BaseModel):
    threads: list[ThreadView]
    more: bool
    archived_count: int
    updates_connected: bool
    watch: WatchHealth


@pytest.fixture
async def seeded(namespace: SandboxNamespace) -> None:
    await namespace.runner("runner-1")
    await namespace.sandbox("shelved", running=False)
    await namespace.egress_policy_set("github", [{"hosts": ["api.github.com"], "methods": ["GET"]}])
    await namespace.policy_binding("runner-1-picked", subject=namespace.account("runner-1"), policy_sets=["github"])
    await namespace.policy_binding("elsewhere", subject=namespace.account("shelved"), policy_sets=["github"])


@pytest.fixture
def event_logs(history: History, namespace: SandboxNamespace) -> SessionHistory:
    return SessionHistory(history, sandbox_namespaces={"runner-1": namespace.name, "shelved": namespace.name})


@pytest.fixture
def egress_admin() -> FakeEgressAdmin:
    return FakeEgressAdmin()


@pytest.fixture
def egress_token_file(tmp_path: Path) -> Path:
    token = tmp_path / "egress-token"
    token.write_text("fake-token")
    return token


@pytest.fixture
async def app_config(
    namespace: SandboxNamespace, seeded: None, egress_admin: FakeEgressAdmin, egress_token_file: Path
) -> AsyncIterator[dict[str, object]]:
    server = grpc.aio.server()
    egress_admin_pb2_grpc.add_EgressAdminServicer_to_server(egress_admin, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield {
            "sandbox_namespace": namespace.name,
            "egress_admin_url": f"http://127.0.0.1:{port}",
            "egress_admin_token_file": str(egress_token_file),
        }


async def _snapshots(response: httpx.Response) -> AsyncIterator[str]:
    response.raise_for_status()
    event = ""
    async for line in response.aiter_lines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: ") and event == "snapshot":
            yield line.removeprefix("data: ")


async def _next_threads(
    stream: AsyncIterator[str], predicate: Callable[[ThreadsSnapshot], bool] = lambda _: True
) -> ThreadsSnapshot:
    async for data in stream:
        frame = ThreadsSnapshot.model_validate_json(data)
        if predicate(frame):
            return frame
    raise AssertionError("thread stream ended before the expected snapshot")


async def _next_decisions(stream: AsyncIterator[str]) -> DecisionsSnapshot:
    return DecisionsSnapshot.model_validate_json(await anext(stream))


async def test_the_streams_need_a_caller(app: AppProcess) -> None:
    async with httpx.AsyncClient(base_url=app.url) as unauthenticated:
        for route in (
            "/live/sandboxes",
            "/live/sandboxes/runner-1/egress/decisions",
            "/live/sandboxes/runner-1",
            "/live/threads",
        ):
            assert (await unauthenticated.get(route)).status_code == 401


async def test_threads_the_history_service_cannot_list_are_said_so_in_the_frame(
    db_url: str,
    history_client: HistoryServiceClient,
    namespace: SandboxNamespace,
    api_server: KubernetesApiServer,
    app_config: dict[str, object],
    refused_address: str,
) -> None:
    async with (
        app_process(
            db_url,
            history_client,
            sandboxes=SandboxesAccess(api_server.kubeconfig, namespace.name),
            gateway=None,
            config=app_config | {"history_service_target": refused_address, "history_service_request_timeout_s": 0.5},
        ) as app,
        httpx.AsyncClient(base_url=app.url, cookies=app.cookies, headers={"Origin": app.url}) as http,
        asyncio.timeout(10),
        http.stream("GET", "/live/sandboxes/runner-1") as response,
    ):
        snapshot = json.loads(await anext(_snapshots(response)))
        assert snapshot["sandbox"]["name"] == "runner-1"
        assert snapshot["threads"] == {"kind": "unavailable", "code": "UNAVAILABLE"}


async def test_global_thread_stream_follows_replica_commits(
    http: httpx.AsyncClient,
    namespace: SandboxNamespace,
    event_logs: SessionHistory,
    db_url: str,
    history_client: HistoryServiceClient,
    api_server: KubernetesApiServer,
    app_config: dict[str, object],
) -> None:
    async with (
        app_process(
            db_url,
            history_client,
            sandboxes=SandboxesAccess(api_server.kubeconfig, namespace.name),
            gateway=None,
            config=app_config,
        ) as replica,
        httpx.AsyncClient(base_url=replica.url, cookies=replica.cookies, headers={"Origin": replica.url}) as other,
    ):
        store = ThreadHTTP(other)
        async with (
            asyncio.timeout(15),
            http.stream("GET", "/live/threads", params={"include_archived": "true"}) as response,
        ):
            stream = _snapshots(response)
            initial = await _next_threads(stream)
            assert initial.threads == []
            assert initial.updates_connected
            # A Thread of a Sandbox without a Pod archives without asking a runner.
            thread_id = await event_logs.seed("shelved", "test-global-thread")
            seeded = await _next_threads(stream, lambda frame: bool(frame.threads))
            assert [thread.id for thread in seeded.threads] == [thread_id]
            await store.rename(thread_id, "Renamed in another app replica")
            await _next_threads(stream, lambda frame: frame.threads[0].name == "Renamed in another app replica")
            await store.archive(thread_id)
            archived = await _next_threads(stream, lambda frame: frame.threads[0].archived)
            assert archived.archived_count == 1


async def test_global_thread_stream_carries_its_window_and_the_open_thread(
    http: httpx.AsyncClient, event_logs: SessionHistory, store: ThreadHTTP
) -> None:
    oldest, middle, newest = [await event_logs.seed("runner-1", f"test-window-thread-{n}") for n in range(3)]
    for thread_id in (oldest, middle, newest):
        await store.wait(thread_id)
    async with (
        asyncio.timeout(10),
        http.stream("GET", "/live/threads", params={"limit": 1, "open_thread": str(oldest)}) as response,
    ):
        window = await _next_threads(_snapshots(response))
        assert ([thread.id for thread in window.threads], window.more) == ([newest, oldest], True)


async def test_global_thread_stream_reports_listener_loss_then_rereads_after_reconnect(
    event_logs: SessionHistory,
    engine: AsyncEngine,
    database_reconnect_gate: tuple[str, asyncio.Event],
    history_client: HistoryServiceClient,
    namespace: SandboxNamespace,
    app_config: dict[str, object],
) -> None:
    database_url, reconnect = database_reconnect_gate
    async with (
        app_process(
            database_url,
            history_client,
            sandboxes=SandboxesAccess(namespace.kubeconfig, namespace.name),
            gateway=None,
            config=app_config,
        ) as app,
        httpx.AsyncClient(base_url=app.url, cookies=app.cookies, headers={"Origin": app.url}) as http,
        asyncio.timeout(10),
        http.stream("GET", "/live/threads") as response,
    ):
        stream = _snapshots(response)
        assert (await _next_threads(stream)).updates_connected
        # Keep the established pool connections alive, but hold the listener's replacement
        # connection until the browser has observed the outage.
        reconnect.clear()
        async with engine.begin() as connection:
            terminated = await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'idle' AND query ILIKE '%LISTEN%'"
                )
            )
            assert terminated.scalars().all() == [True]
        await _next_threads(stream, lambda frame: not frame.updates_connected)
        thread_id = await event_logs.seed("runner-1", "test-during-listener-gap")
        reconnect.set()
        recovered = await _next_threads(stream, lambda frame: frame.updates_connected and bool(frame.threads))
        assert [thread.id for thread in recovered.threads] == [thread_id]


@pytest.fixture
async def database_reconnect_gate(db_url: str) -> AsyncIterator[tuple[str, asyncio.Event]]:
    """Hold new TCP connections without interrupting the app's existing PostgreSQL pool."""
    target = make_url(db_url)
    database_host, database_port = target.host, target.port
    assert database_host is not None
    assert database_port is not None
    reconnect = asyncio.Event()
    reconnect.set()
    writers: set[asyncio.StreamWriter] = set()

    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.add(writer)
        try:
            await reconnect.wait()
            upstream_reader, upstream_writer = await asyncio.open_connection(database_host, database_port)
            writers.add(upstream_writer)
            try:
                outgoing = asyncio.create_task(pipe(reader, upstream_writer))
                incoming = asyncio.create_task(pipe(upstream_reader, writer))
                try:
                    await asyncio.wait((outgoing, incoming), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    outgoing.cancel()
                    incoming.cancel()
                    await asyncio.gather(outgoing, incoming, return_exceptions=True)
            finally:
                upstream_writer.close()
                writers.discard(upstream_writer)
        finally:
            writer.close()
            writers.discard(writer)

    async with asyncio.TaskGroup() as tasks:
        server = await asyncio.start_server(
            lambda reader, writer: tasks.create_task(relay(reader, writer)), "127.0.0.1", 0
        )
        async with server:
            try:
                yield (
                    target.set(host="127.0.0.1", port=server.sockets[0].getsockname()[1]).render_as_string(
                        hide_password=False
                    ),
                    reconnect,
                )
            finally:
                reconnect.set()
                for writer in tuple(writers):
                    writer.close()


async def test_egress_decision_stream_receives_snapshots_and_recovers(
    http: httpx.AsyncClient, namespace: SandboxNamespace, egress_admin: FakeEgressAdmin
) -> None:
    subject = (namespace.name, "runner-1")
    egress_admin.decisions[subject] = [decision("2026-09-02T10:00:01Z", "GET", "api.github.com", "allow")]
    async with asyncio.timeout(10), http.stream("GET", "/live/sandboxes/runner-1/egress/decisions") as response:
        stream = _snapshots(response)
        first = await _next_decisions(stream)
        assert isinstance(first.decisions, list)
        assert first.watching
        assert len(first.decisions) == 1
        admitted = first.decisions[0]
        assert (admitted.method, admitted.host) == ("GET", "api.github.com")
        assert admitted.at == datetime(2026, 9, 2, 10, 0, 1, tzinfo=UTC)
        assert admitted.outcome is Outcome.ALLOW
        assert admitted.rule == 0
        assert admitted.reason is None
        egress_admin.decisions[subject].append(decision("2026-09-02T10:00:02Z", "POST", "pypi.org", "deny"))
        egress_admin.notify()
        second = await _next_decisions(stream)
        assert isinstance(second.decisions, list)
        assert second.watching
        assert [d.host for d in second.decisions] == ["api.github.com", "pypi.org"]
        assert second.decisions[1].outcome is Outcome.DENY
        assert second.decisions[1].reason is None
        egress_admin.reachable = False
        egress_admin.notify()
        unavailable = await _next_decisions(stream)
        assert not unavailable.watching
        assert isinstance(unavailable.decisions, DecisionsUnavailable)
        egress_admin.decisions[subject].append(decision("2026-09-02T10:00:03Z", "GET", "pypi.org", "allow"))
        egress_admin.reachable = True
        recovered = await _next_decisions(stream)
        assert recovered.watching
        assert isinstance(recovered.decisions, list)
        assert [d.host for d in recovered.decisions] == ["api.github.com", "pypi.org", "pypi.org"]


async def test_egress_decision_stream_reconnects_after_lease_with_rotated_token(
    http: httpx.AsyncClient, namespace: SandboxNamespace, egress_admin: FakeEgressAdmin, egress_token_file: Path
) -> None:
    subject = (namespace.name, "runner-1")
    egress_admin.decisions[subject] = [decision("2026-09-02T10:00:01Z", "GET", "api.github.com", "allow")]
    async with asyncio.timeout(10), http.stream("GET", "/live/sandboxes/runner-1/egress/decisions") as response:
        stream = _snapshots(response)
        assert (await _next_decisions(stream)).watching
        assert egress_admin.tokens == ["Bearer fake-token"]
        await asyncio.to_thread(egress_token_file.write_text, "rotated-token")
        egress_admin.expire_lease()
        assert (await _next_decisions(stream)).watching
        assert egress_admin.tokens == ["Bearer fake-token", "Bearer rotated-token"]


async def test_egress_decision_stream_says_a_sandbox_is_missing(http: httpx.AsyncClient) -> None:
    async with asyncio.timeout(10), http.stream("GET", "/live/sandboxes/nope/egress/decisions") as response:
        missing = await _next_decisions(_snapshots(response))
        assert not missing.watching
        assert isinstance(missing.decisions, DecisionsUnavailable)


if __name__ == "__main__":
    pytest_bazel.main()

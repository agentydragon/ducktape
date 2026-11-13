"""Live views through a real Go HTTP server, Kubernetes watches and PostgreSQL LISTEN."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import grpc
import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import ParseDict
from kubernetes_asyncio import client as k8s_client
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.testing.egress_proxy import FakeEgressAdmin, decision
from agentplane.app.testing.history import SessionHistory
from agentplane.app.testing.replication_process import AppProcess, GatewayAccess, SandboxesAccess, app_process
from agentplane.app.testing.runner_gateway import APP, DISPATCHER_AUDIENCE, GATEWAY, GATEWAY_AUDIENCE
from agentplane.app.testing.thread_test_support import ThreadHTTP
from agentplane.egress.api.admin import egress_admin_pb2, egress_admin_pb2_grpc
from agentplane.grpc.serving import serving
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.history import History
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess, run_gateway
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer

# gazelle:include_dep //agentplane/app:integration_fixtures
pytest_plugins = ("agentplane.app.integration_fixtures",)


@pytest.fixture
async def seeded(namespace: SandboxNamespace) -> None:
    runner, _ = await namespace.runner("runner-1")
    await namespace.core_v1.create_namespaced_service_account(
        namespace.name,
        k8s_client.V1ServiceAccount(
            metadata=k8s_client.V1ObjectMeta(
                name="runner-1",
                owner_references=[
                    k8s_client.V1OwnerReference(
                        api_version=SANDBOX_API_VERSION,
                        kind=SANDBOX_KIND,
                        name="runner-1",
                        uid=runner["metadata"]["uid"],
                        controller=True,
                    )
                ],
            )
        ),
    )
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


@pytest.fixture
async def gateway_service(
    namespace: SandboxNamespace, api_server: KubernetesApiServer, dispatcher_database_url: str, tmp_path: Path
) -> AsyncIterator[GatewayProcess]:
    workload = ServiceAccountRef(namespace=namespace.name, name=GATEWAY.name)
    gateway_token_file = tmp_path / "runner-gateway-token"
    gateway_token_file.write_text(await api_server.workload_token(workload, [GATEWAY_AUDIENCE]))
    async with run_gateway(
        api_server.kubeconfig,
        database_url=dispatcher_database_url,
        namespace=namespace.name,
        dispatcher_callers=(APP,),
        gateway_callers=(APP, workload),
        workload=workload,
        environment_readers=(APP,),
        dispatcher_audience=DISPATCHER_AUDIENCE,
        gateway_audience=GATEWAY_AUDIENCE,
        runner_proxy_token_file=gateway_token_file,
    ) as service:
        yield service


@pytest.fixture
def namespace_gateway(gateway_service: GatewayProcess, app_token_file: Path) -> GatewayAccess:
    return GatewayAccess(gateway_service.target, app_token_file, gateway_service.client_tls)


async def _watch_results(response: httpx.Response) -> AsyncIterator[dict[str, object]]:
    response.raise_for_status()
    async for line in response.aiter_lines():
        if line:
            envelope = json.loads(line)
            result = envelope.get("result") if isinstance(envelope, dict) else None
            if not isinstance(result, dict):
                raise AssertionError(f"watch returned an invalid result envelope: {envelope!r}")
            yield result


async def _wait_updates_connected(stream: AsyncIterator[dict[str, object]], *, connected: bool) -> dict[str, object]:
    async for result in stream:
        if result["updatesConnected"] is connected:
            return result
    raise AssertionError(f"watch ended before updatesConnected became {connected}")


async def test_the_streams_need_a_caller(app: AppProcess) -> None:
    async with httpx.AsyncClient(base_url=app.url) as unauthenticated:
        for route in (
            "/upstream/environments/v1/environments:watch",
            "/upstream/egress/v1/decisions:watch",
            "/upstream/policy/v1/policy-bindings:watch",
            "/upstream/policy/v1/policy-sets:watch",
            "/upstream/app/v1/threads",
            "/upstream/app/v1/threads:watch",
            "/upstream/history/v1/session-summaries:watch",
        ):
            assert (await unauthenticated.get(route)).status_code == 401


async def test_environment_thread_rpc_reports_history_unavailable(
    db_url: str,
    history_client: HistoryServiceClient,
    namespace: SandboxNamespace,
    api_server: KubernetesApiServer,
    app_config: dict[str, object],
    refused_address: str,
    namespace_gateway: GatewayAccess,
) -> None:
    async with (
        app_process(
            db_url,
            history_client,
            sandboxes=SandboxesAccess(api_server.kubeconfig, namespace.name),
            gateway=namespace_gateway,
            config=app_config | {"history_service_target": refused_address, "history_service_request_timeout_s": 0.5},
        ) as app,
        httpx.AsyncClient(base_url=app.url, cookies=app.cookies, headers={"Origin": app.url}) as http,
        asyncio.timeout(10),
    ):
        environments = await http.get("/upstream/environments/v1/environments", params={"limit": 100})
        assert environments.status_code == 200, environments.text
        uid = next(
            item["target"]["uid"] for item in environments.json()["items"] if item["target"]["name"] == "runner-1"
        )
        response = await http.get("/upstream/app/v1/threads", params={"sandbox_uid": uid})
        assert response.status_code == 503, response.text


async def test_thread_metadata_watch_follows_replica_commits(
    http: httpx.AsyncClient,
    namespace: SandboxNamespace,
    event_logs: SessionHistory,
    db_url: str,
    history_client: HistoryServiceClient,
    api_server: KubernetesApiServer,
    app_config: dict[str, object],
    namespace_gateway: GatewayAccess,
) -> None:
    async with (
        app_process(
            db_url,
            history_client,
            sandboxes=SandboxesAccess(api_server.kubeconfig, namespace.name),
            gateway=namespace_gateway,
            config=app_config,
        ) as replica,
        httpx.AsyncClient(base_url=replica.url, cookies=replica.cookies, headers={"Origin": replica.url}) as other,
    ):
        store = ThreadHTTP(other)
        async with (
            asyncio.timeout(15),
            http.stream(
                "GET", "/upstream/app/v1/threads:watch", headers={"Accept": "application/x-ndjson"}
            ) as response,
        ):
            stream = _watch_results(response)
            initial = await anext(stream)
            assert initial["changed"] == {}
            assert initial["updatesConnected"] is True
            # A Thread of a Sandbox without a Pod archives without asking a runner.
            thread_id = await event_logs.seed("shelved", "test-global-thread")
            assert (await anext(stream))["changed"] == {}
            await store.wait(thread_id)
            page = (await http.get("/upstream/app/v1/threads", params={"include_archived": "true"})).json()
            assert [thread["threadId"] for thread in page["threads"]] == [str(thread_id)]
            await store.rename(thread_id, "Renamed in another app replica")
            assert (await anext(stream))["changed"] == {}
            page = (await http.get("/upstream/app/v1/threads", params={"include_archived": "true"})).json()
            assert page["threads"][0]["name"] == "Renamed in another app replica"
            await store.archive(thread_id)
            assert (await anext(stream))["changed"] == {}
            page = (await http.get("/upstream/app/v1/threads", params={"include_archived": "true"})).json()
            assert page["threads"][0]["archived"] is True
            assert page["archivedCount"] == 1


async def test_thread_metadata_list_returns_newest_page_and_archived_count(
    http: httpx.AsyncClient, event_logs: SessionHistory, store: ThreadHTTP
) -> None:
    oldest, middle, newest = [await event_logs.seed("runner-1", f"test-window-thread-{n}") for n in range(3)]
    for thread_id in (oldest, middle, newest):
        await store.wait(thread_id)
    page = (await http.get("/upstream/app/v1/threads", params={"limit": 1})).json()
    assert ([thread["threadId"] for thread in page["threads"]], bool(page["nextPageToken"])) == ([str(newest)], True)
    assert page.get("archivedCount", 0) == 0
    page = (await http.get("/upstream/app/v1/threads", params={"limit": 1, "page_token": page["nextPageToken"]})).json()
    assert ([thread["threadId"] for thread in page["threads"]], bool(page["nextPageToken"])) == ([str(middle)], True)
    page = (await http.get("/upstream/app/v1/threads", params={"limit": 1, "page_token": page["nextPageToken"]})).json()
    assert ([thread["threadId"] for thread in page["threads"]], page.get("nextPageToken", "")) == ([str(oldest)], "")


async def test_thread_metadata_watch_reports_listener_loss_and_reconnect(
    event_logs: SessionHistory,
    engine: AsyncEngine,
    database_reconnect_gate: tuple[str, asyncio.Event],
    history_client: HistoryServiceClient,
    namespace: SandboxNamespace,
    app_config: dict[str, object],
    namespace_gateway: GatewayAccess,
) -> None:
    database_url, reconnect = database_reconnect_gate
    async with (
        app_process(
            database_url,
            history_client,
            sandboxes=SandboxesAccess(namespace.kubeconfig, namespace.name),
            gateway=namespace_gateway,
            config=app_config,
        ) as app,
        httpx.AsyncClient(base_url=app.url, cookies=app.cookies, headers={"Origin": app.url}) as http,
        asyncio.timeout(10),
        http.stream("GET", "/upstream/app/v1/threads:watch", headers={"Accept": "application/x-ndjson"}) as response,
    ):
        stream = _watch_results(response)
        assert (await anext(stream))["updatesConnected"] is True
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
        # A healthy metadata invalidation may already be buffered before the listener loss.
        # The watch reports current state on each wakeup, not only connection transitions.
        await _wait_updates_connected(stream, connected=False)
        thread_id = await event_logs.seed("runner-1", "test-during-listener-gap")
        reconnect.set()
        recovered = await _wait_updates_connected(stream, connected=True)
        assert recovered["updatesConnected"] is True
        page = (await http.get("/upstream/app/v1/threads")).json()
        assert [thread["threadId"] for thread in page["threads"]] == [str(thread_id)]


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


async def test_generated_egress_watch_streams_decisions_and_rotates_token(
    http: httpx.AsyncClient, namespace: SandboxNamespace, egress_admin: FakeEgressAdmin, egress_token_file: Path
) -> None:
    subject = (namespace.name, "runner-1")
    egress_admin.decisions[subject] = [decision("2026-09-02T10:00:01Z", "GET", "api.github.com", "allow")]
    path = "/upstream/egress/v1/decisions:watch"
    params = {"filter.subject.namespace": subject[0], "filter.subject.name": subject[1]}
    async with asyncio.timeout(10), http.stream("GET", path, params=params) as response:
        stream = _watch_results(response)
        first = ParseDict(await anext(stream), egress_admin_pb2.WatchDecisionsResponse())
        assert first.WhichOneof("frame") == "snapshot"
        (admitted,) = first.snapshot.decisions
        assert (admitted.method, admitted.host) == ("GET", "api.github.com")
        assert admitted.at.ToDatetime(tzinfo=UTC) == datetime(2026, 9, 2, 10, 0, 1, tzinfo=UTC)
        assert admitted.outcome == egress_admin_pb2.Decision.OUTCOME_ALLOW
        assert admitted.HasField("rule")
        assert admitted.rule == 0
        assert not admitted.HasField("reason")
        egress_admin.decisions[subject].append(decision("2026-09-02T10:00:02Z", "POST", "pypi.org", "deny"))
        egress_admin.notify()
        second = ParseDict(await anext(stream), egress_admin_pb2.WatchDecisionsResponse())
        assert second.WhichOneof("frame") == "delta"
        (refused,) = second.delta.added
        assert refused.host == "pypi.org"
        assert refused.outcome == egress_admin_pb2.Decision.OUTCOME_DENY
    assert egress_admin.tokens == ["Bearer fake-token"]
    await asyncio.to_thread(egress_token_file.write_text, "rotated-token")
    async with asyncio.timeout(10), http.stream("GET", path, params=params) as response:
        frame = ParseDict(await anext(_watch_results(response)), egress_admin_pb2.WatchDecisionsResponse())
        assert len(frame.snapshot.decisions) == 2
    assert egress_admin.tokens == ["Bearer fake-token", "Bearer rotated-token"]


if __name__ == "__main__":
    pytest_bazel.main()

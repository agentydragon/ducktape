"""Black-box tests of the built Go `egress-proxy` process.

A real client goes through the binary to a scripted upstream. The proxy intercepts with a throwaway
CA the client trusts, verifies the upstream's certificate against a second throwaway CA, takes its
identity and policy from a real envtest API server, and appends decisions to PostgreSQL.
The replica-divergence fault injection uses scripted API servers. Every assertion
is on what the client and the upstream actually saw.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path
from typing import cast

import aiohttp
import grpc
import pytest
import pytest_bazel
from aiohttp import web
from kubernetes_asyncio import client as k8s_client
from more_itertools import one
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.conftest import AUDIENCE, GITHUB_POLICY, SANDBOX_A, SUBJECT_A, TOKEN_A, UPSTREAM_HOST, seed
from agentplane.egress.resources import TargetMethod, placeholder_of
from agentplane.egress.settings import Settings
from agentplane.egress.testing.go_replica import go_replica, kubeconfig
from agentplane.egress.testing.sidecar_process import SidecarProcess
from agentplane.egress.testing.tls import (
    CertificateAuthority,
    client_tls_context,
    issue_leaf,
    make_ca,
    server_tls_context,
    write_ca,
)
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.testing.fake_apiserver import (
    CREDENTIALS_NAMESPACE,
    CREDENTIALS_PLURAL,
    NAMESPACE,
    SANDBOX_NAMESPACE,
    credential,
    egress_policy_set,
    fake_apiserver,
    policy_binding,
)
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, kubernetes_apiserver
from util.kubernetes import CustomObjectsClient


@dataclass
class RecordingUpstream:
    """HTTP and HTTPS listeners that record every request's method, path, and headers."""

    slow_started: asyncio.Event = field(default_factory=asyncio.Event)
    slow_release: asyncio.Event = field(default_factory=asyncio.Event)
    port: int = 0
    http_port: int = 0
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list, repr=False)

    async def handle(self, request: web.Request) -> web.Response:
        if request.path == "/public/slow":
            self.slow_started.set()
            await self.slow_release.wait()
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.requests.append((request.method, request.path_qs, headers))
        self.bodies.append(await request.read())
        return web.Response(text="upstream ok")


@asynccontextmanager
async def recording_upstream(cert_path: Path, key_path: Path) -> AsyncIterator[RecordingUpstream]:
    upstream = RecordingUpstream()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", upstream.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    https_site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls_context(cert_path, key_path))
    await https_site.start()
    upstream.port = one(runner.addresses)[1]
    http_site = web.TCPSite(runner, "127.0.0.1", 0)
    await http_site.start()
    upstream.http_port = one(address[1] for address in runner.addresses if address[1] != upstream.port)
    try:
        yield upstream
    finally:
        await runner.cleanup()


def replica_settings(
    *,
    api_config: Path,
    ca: tuple[Path, Path],
    database_url: str,
    port_factory: Callable[[], int],
    upstream_ca_file: Path | None = None,
) -> Settings:
    cert, key = ca
    return Settings(
        _cli_parse_args=False,
        rules_namespace=NAMESPACE,
        allowed_service_account_namespaces=frozenset({SANDBOX_NAMESPACE}),
        credentials_namespace=CREDENTIALS_NAMESPACE,
        kubeconfig=api_config,
        ca_cert=cert,
        ca_key=key,
        listen_host="127.0.0.1",
        listen_port=port_factory(),
        admin_host="127.0.0.1",
        admin_port=port_factory(),
        agent_api_host="127.0.0.1",
        agent_api_port=port_factory(),
        token_audience=AUDIENCE,
        upstream_ca_file=upstream_ca_file,
        # The Go informer's watch period is whole seconds, two at the least.
        resync_seconds=2,
        database_url=database_url,
        exempt_networks=[ip_network("127.0.0.0/8"), ip_network("::1/128")],
    )


BINDING = f"{SANDBOX_A}-{GITHUB_POLICY}"
_CONTRACT_CREDENTIAL = "transport-contract"
_CONTRACT_PATH = "/google.devtools.build.v1.PublishBuildEvent/PublishBuildToolEventStream"


async def seed_transport_contract(server: KubernetesApiServer) -> str:
    core = k8s_client.CoreV1Api(server.api)
    for namespace in (NAMESPACE, SANDBOX_NAMESPACE, CREDENTIALS_NAMESPACE):
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace)))
    await core.create_namespaced_service_account(
        SANDBOX_NAMESPACE, k8s_client.V1ServiceAccount(metadata=k8s_client.V1ObjectMeta(name=SANDBOX_A))
    )
    await core.create_namespaced_pod(
        SANDBOX_NAMESPACE,
        k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(name=SANDBOX_A),
            spec=k8s_client.V1PodSpec(
                service_account_name=SANDBOX_A,
                containers=[k8s_client.V1Container(name="workload", image="registry.test/unused")],
            ),
        ),
    )
    await core.create_namespaced_secret(
        CREDENTIALS_NAMESPACE,
        k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=_CONTRACT_CREDENTIAL), string_data={"key": "contract-secret"}
        ),
    )
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(server.api))
    for plural, body in (
        (
            CREDENTIALS_PLURAL,
            credential(
                _CONTRACT_CREDENTIAL,
                secret_name=_CONTRACT_CREDENTIAL,
                key="key",
                targets=[{"header": "x-buildbuddy-api-key", "method": TargetMethod.WHOLE_VALUE}],
            ),
        ),
        (
            POLICY_SETS_PLURAL,
            egress_policy_set(
                _CONTRACT_CREDENTIAL,
                [
                    {
                        "hosts": [UPSTREAM_HOST],
                        "paths": ["/contract", "/contract-sse", "/contract-ws", _CONTRACT_PATH],
                        "credentialRef": {"name": _CONTRACT_CREDENTIAL},
                    }
                ],
            ),
        ),
        (
            POLICY_BINDINGS_PLURAL,
            policy_binding(BINDING, subject=SUBJECT_A.model_dump(), policy_sets=[_CONTRACT_CREDENTIAL]),
        ),
    ):
        await custom.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, body)
    return await server.pod_token(SANDBOX_NAMESPACE, SANDBOX_A, [AUDIENCE])


@dataclass(frozen=True)
class Egress:
    """The running proxy and the scripted world around it."""

    proxy_port: int
    token: str
    api: KubernetesApiServer
    interception_ca: CertificateAuthority
    upstream_ca: CertificateAuthority
    upstream: RecordingUpstream
    tmp_path: Path

    def url(self, path: str) -> str:
        return f"https://{UPSTREAM_HOST}:{self.upstream.port}{path}"


@pytest.fixture
async def egress(
    tmp_path: Path, history_db_url: str, unused_tcp_port_factory: Callable[[], int]
) -> AsyncIterator[Egress]:
    interception_ca = make_ca("agentplane-egress-test-interception")
    upstream_ca = make_ca("agentplane-egress-test-upstream")
    async with (
        kubernetes_apiserver() as api,
        recording_upstream(*issue_leaf(upstream_ca, UPSTREAM_HOST, tmp_path)) as upstream,
    ):
        token = await seed_transport_contract(api)
        settings = replica_settings(
            api_config=api.kubeconfig,
            ca=write_ca(interception_ca, tmp_path, "interception"),
            database_url=history_db_url,
            port_factory=unused_tcp_port_factory,
            upstream_ca_file=write_ca(upstream_ca, tmp_path, "upstream")[0],
        )
        async with go_replica(settings, directory=tmp_path) as running:
            yield Egress(
                token=token,
                api=api,
                proxy_port=running.proxy_port,
                interception_ca=interception_ca,
                upstream_ca=upstream_ca,
                upstream=upstream,
                tmp_path=tmp_path,
            )


@asynccontextmanager
async def recording_grpc_upstream(
    keypair: tuple[bytes, bytes] | None, *, finished: asyncio.Event
) -> AsyncIterator[tuple[int, asyncio.Queue[tuple[tuple[str, str], ...]]]]:
    """A gRPC server recording the metadata of a bidirectional call: TLS with a keypair, else plaintext."""
    requests: asyncio.Queue[tuple[tuple[str, str], ...]] = asyncio.Queue()

    async def stream(request_iterator: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
        metadata = context.invocation_metadata() or ()
        await requests.put(tuple((str(item[0]), str(item[1])) for item in metadata))
        try:
            async for request in request_iterator:
                yield b"upstream:" + request
            context.set_trailing_metadata((("buildbuddy-test-trailer", "ok"),))
        finally:
            finished.set()

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "google.devtools.build.v1.PublishBuildEvent",
                {
                    "PublishBuildToolEventStream": grpc.stream_stream_rpc_method_handler(
                        stream, request_deserializer=lambda value: value, response_serializer=lambda value: value
                    )
                },
            ),
        )
    )
    port = (
        server.add_insecure_port("127.0.0.1:0")
        if keypair is None
        else server.add_secure_port("127.0.0.1:0", grpc.ssl_server_credentials([(keypair[1], keypair[0])]))
    )
    await server.start()
    try:
        yield port, requests
    finally:
        await server.stop(grace=None)


def read_keypair(cert_path: Path, key_path: Path) -> tuple[bytes, bytes]:
    return cert_path.read_bytes(), key_path.read_bytes()


async def test_health_reports_ready_only_once_the_tunnel_accepts(
    tmp_path: Path, history_db_url: str, unused_tcp_port_factory: Callable[[], int]
) -> None:
    """Readiness gates every Service this Pod backs, and the tunnel is the one that carries traffic."""
    ca = write_ca(make_ca("test-readiness-ca"), tmp_path, "readiness")
    async with fake_apiserver() as api:
        seed(api)
        settings = replica_settings(
            api_config=kubeconfig(tmp_path / "fake-api.yaml", api.port),
            ca=ca,
            database_url=history_db_url,
            port_factory=unused_tcp_port_factory,
        )
        async with go_replica(settings, directory=tmp_path) as running:
            _, writer = await asyncio.open_connection("127.0.0.1", running.proxy_port)
            writer.close()
            await writer.wait_closed()


async def test_two_process_replicas_diverge_fail_closed_and_share_decisions(
    tmp_path: Path, history_db_url: str, unused_tcp_port_factory: Callable[[], int]
) -> None:
    ca = make_ca("test-replica-ca")
    cert, key = write_ca(ca, tmp_path, "replica")
    async with (
        fake_apiserver() as first_api,
        fake_apiserver() as second_api,
        recording_upstream(*issue_leaf(ca, UPSTREAM_HOST, tmp_path)) as upstream,
    ):
        seed(first_api)
        seed(second_api)
        settings = [
            replica_settings(
                api_config=kubeconfig(tmp_path / f"fake-api-{i}.yaml", fake.port),
                ca=(cert, key),
                database_url=history_db_url,
                port_factory=unused_tcp_port_factory,
            )
            for i, fake in enumerate([first_api, second_api])
        ]
        async with (
            go_replica(settings[0], directory=tmp_path) as first,
            go_replica(settings[1], directory=tmp_path) as second,
            aiohttp.ClientSession() as client,
        ):

            async def get(port: int, path: str, expected: int) -> None:
                async with client.get(
                    f"http://{UPSTREAM_HOST}:{upstream.http_port}/public/{path}",
                    proxy=f"http://127.0.0.1:{port}",
                    headers={"Proxy-Authorization": f"Bearer {TOKEN_A}"},
                ) as response:
                    await response.read()
                    assert response.status == expected, dict(response.headers)

            await get(first.proxy_port, "first", 200)
            await get(second.proxy_port, "second", 200)
            # Only one watch source observes revocation. No replica can acknowledge the other.
            first_api.delete(POLICY_BINDINGS_PLURAL, BINDING)
            async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
                with attempt:
                    await get(first.proxy_port, "revoked", 403)
            await get(second.proxy_port, "still-current", 200)
            second_api.watch_available = False
            second_api.close_watches()
            await second.health(503)
            before = len(upstream.requests)
            await get(second.proxy_port, "stale-existing-connection", 502)
            assert len(upstream.requests) == before
            second_api.watch_available = True
            second_api.delete(POLICY_BINDINGS_PLURAL, BINDING)
            await second.health(200)
            await get(second.proxy_port, "recovered-revocation", 403)
            async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
                with attempt:
                    # Either replica serves the history both wrote.
                    async with client.get(
                        f"http://127.0.0.1:{first.admin_port}/decisions", params=SUBJECT_A.model_dump()
                    ) as response:
                        rows = await response.json()
                    producers = {row["producer_id"] for row in rows}
                    assert len(producers) == 2
                    assert all(
                        len({row["connection_id"] for row in rows if row["producer_id"] == producer}) == 1
                        for producer in producers
                    )
                    assert any(row["reason"] == "unavailable" for row in rows)


async def test_go_proxy_finishes_an_admitted_request_after_sigterm(
    tmp_path: Path, history_db_url: str, unused_tcp_port_factory: Callable[[], int]
) -> None:
    """Termination takes the Pod out of rotation at once, yet lets the request already admitted finish."""
    ca = make_ca("test-sigterm-ca")
    cert, key = write_ca(ca, tmp_path, "sigterm")
    async with (
        fake_apiserver() as api,
        recording_upstream(*issue_leaf(ca, UPSTREAM_HOST, tmp_path)) as upstream,
        aiohttp.ClientSession() as client,
    ):
        seed(api)
        settings = replica_settings(
            api_config=kubeconfig(tmp_path / "fake-api.yaml", api.port),
            ca=(cert, key),
            database_url=history_db_url,
            port_factory=unused_tcp_port_factory,
        )
        async with go_replica(settings, directory=tmp_path) as running:

            async def slow_status() -> int:
                async with client.get(
                    f"http://{UPSTREAM_HOST}:{upstream.http_port}/public/slow",
                    proxy=f"http://127.0.0.1:{running.proxy_port}",
                    headers={"Proxy-Authorization": f"Bearer {TOKEN_A}"},
                ) as response:
                    await response.read()
                    return response.status

            pending = asyncio.create_task(slow_status())
            await asyncio.wait_for(upstream.slow_started.wait(), 10)
            running.terminate()
            await running.health(503)
            # The readiness endpoint turns 503 before the proxy listener finishes shutting down.
            async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
                with attempt:
                    try:
                        _, writer = await asyncio.open_connection("127.0.0.1", running.proxy_port)
                    except ConnectionRefusedError:
                        break
                    writer.close()
                    await writer.wait_closed()
                    raise AssertionError("proxy listener still accepts connections while draining")
            assert not pending.done()
            upstream.slow_release.set()
            assert await asyncio.wait_for(pending, 10) == 200
    assert len(upstream.requests) == 1


async def test_header_substitution(egress: Egress) -> None:
    async with (
        aiohttp.ClientSession() as client,
        client.get(
            egress.url("/contract"),
            proxy=f"http://127.0.0.1:{egress.proxy_port}",
            proxy_headers={"Proxy-Authorization": f"Bearer {egress.token}"},
            ssl=client_tls_context(egress.interception_ca),
            headers={"x-buildbuddy-api-key": placeholder_of(_CONTRACT_CREDENTIAL)},
        ) as response,
    ):
        assert response.status == 200
        await response.read()
    assert one(egress.upstream.requests)[2]["x-buildbuddy-api-key"] == "contract-secret"
    assert "proxy-authorization" not in one(egress.upstream.requests)[2]


async def test_real_binding_revocation_denies_the_next_connection(egress: Egress) -> None:
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(egress.api.api))
    async with aiohttp.ClientSession() as client:

        async def request_status() -> int:
            try:
                async with client.get(
                    egress.url("/contract"),
                    proxy=f"http://127.0.0.1:{egress.proxy_port}",
                    proxy_headers={"Proxy-Authorization": f"Bearer {egress.token}"},
                    ssl=client_tls_context(egress.interception_ca),
                    headers={"x-buildbuddy-api-key": placeholder_of(_CONTRACT_CREDENTIAL)},
                ) as response:
                    await response.read()
                    return response.status
            except aiohttp.ClientHttpProxyError as error:
                return error.status

        assert await request_status() == 200
        await custom.delete_namespaced_custom_object(
            GROUP, VERSION, NAMESPACE, POLICY_BINDINGS_PLURAL, BINDING, body=k8s_client.V1DeleteOptions()
        )
        async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
            with attempt:
                assert await request_status() == 403
        before = len(egress.upstream.requests)
        assert await request_status() == 403
        assert len(egress.upstream.requests) == before


async def test_incremental_sse_and_websocket(egress: Egress) -> None:
    finish = asyncio.Event()

    async def sse(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b"data: first\n\n")
        await finish.wait()
        await response.write_eof()
        return response

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        response = web.WebSocketResponse()
        await response.prepare(request)
        async for message in response:
            assert message.type is aiohttp.WSMsgType.TEXT
            await response.send_str(f"upstream:{message.data}")
        return response

    app = web.Application()
    app.router.add_get("/contract-sse", sse)
    app.router.add_get("/contract-ws", websocket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        "127.0.0.1",
        0,
        ssl_context=server_tls_context(*issue_leaf(egress.upstream_ca, UPSTREAM_HOST, egress.tmp_path)),
    )
    await site.start()
    port = one(runner.addresses)[1]
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(
                f"https://{UPSTREAM_HOST}:{port}/contract-sse",
                proxy=f"http://127.0.0.1:{egress.proxy_port}",
                proxy_headers={"Proxy-Authorization": f"Bearer {egress.token}"},
                ssl=client_tls_context(egress.interception_ca),
            ) as response:
                assert response.status == 200
                async with asyncio.timeout(5):
                    assert await response.content.readline() == b"data: first\n"
                # The server is still waiting here: completion cannot explain the read.
                assert not finish.is_set()
                finish.set()
                await response.read()
            async with client.ws_connect(
                f"https://{UPSTREAM_HOST}:{port}/contract-ws",
                proxy=f"http://127.0.0.1:{egress.proxy_port}",
                proxy_headers={"Proxy-Authorization": f"Bearer {egress.token}"},
                ssl=client_tls_context(egress.interception_ca),
            ) as websocket_client:
                await websocket_client.send_str("first")
                async with asyncio.timeout(5):
                    assert (await websocket_client.receive()).data == "upstream:first"
                await websocket_client.send_str("second")
                async with asyncio.timeout(5):
                    assert (await websocket_client.receive()).data == "upstream:second"
    finally:
        finish.set()
        await runner.cleanup()


async def test_denial_has_zero_upstream_tcp_accepts(egress: Egress) -> None:
    accepted: list[asyncio.StreamWriter] = []
    observed = asyncio.Event()

    async def accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(writer)
        observed.set()
        writer.close()
        await writer.wait_closed()

    # Plain raw TCP sees accepts even if the proxy would fail its subsequent TLS
    # handshake. Counting aiohttp handlers would miss this security boundary.
    async with await asyncio.start_server(accept, "127.0.0.1", 0) as upstream:
        port = upstream.sockets[0].getsockname()[1]
        async with (
            aiohttp.ClientSession() as client,
            client.get(
                f"https://{UPSTREAM_HOST}:{port}/not-admitted",
                proxy=f"http://127.0.0.1:{egress.proxy_port}",
                proxy_headers={"Proxy-Authorization": f"Bearer {egress.token}"},
                ssl=client_tls_context(egress.interception_ca),
            ) as response,
        ):
            assert response.status == 403
            await response.read()
        assert accepted == []
        # Positive control proves the accept counter is attached to a live socket.
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        async with asyncio.timeout(5):
            await observed.wait()
        writer.close()
        await writer.wait_closed()
        assert len(accepted) == 1


@pytest.mark.parametrize("tls", [True, False], ids=["tls-h2", "h2c"])
async def test_interactive_grpc_before_eof(egress: Egress, monkeypatch: pytest.MonkeyPatch, tls: bool) -> None:
    keypair = read_keypair(*issue_leaf(egress.upstream_ca, UPSTREAM_HOST, egress.tmp_path)) if tls else None
    token_file = egress.tmp_path / "contract-hop-token"
    token_file.write_text(egress.token)
    finished = asyncio.Event()
    async with (
        recording_grpc_upstream(keypair, finished=finished) as (port, requests),
        SidecarProcess(proxy_host="127.0.0.1", proxy_port=egress.proxy_port, token_file=token_file) as sidecar,
    ):
        monkeypatch.setenv("https_proxy" if tls else "grpc_proxy", f"http://127.0.0.1:{sidecar.listen_port}")
        monkeypatch.setenv("no_proxy", "")
        channel = (
            grpc.aio.secure_channel(
                f"{UPSTREAM_HOST}:{port}",
                grpc.ssl_channel_credentials(root_certificates=egress.interception_ca.cert_pem),
            )
            if tls
            else grpc.aio.insecure_channel(f"{UPSTREAM_HOST}:{port}")
        )
        async with channel:
            call = channel.stream_stream(
                _CONTRACT_PATH, request_serializer=lambda value: value, response_deserializer=lambda value: value
            )(metadata=(("x-buildbuddy-api-key", placeholder_of(_CONTRACT_CREDENTIAL)),), timeout=10)
            for message in (b"first", b"second"):
                await call.write(message)
                assert await call.read() == b"upstream:" + message
            await call.done_writing()
            assert await call.read() is grpc.aio.EOF
            assert dict(await call.trailing_metadata() or ())["buildbuddy-test-trailer"] == "ok"
            assert finished.is_set()
            finished.clear()
            deadline = channel.stream_stream(
                _CONTRACT_PATH, request_serializer=lambda value: value, response_deserializer=lambda value: value
            )(metadata=(("x-buildbuddy-api-key", placeholder_of(_CONTRACT_CREDENTIAL)),), timeout=1)
            await deadline.write(b"before-deadline")
            assert await deadline.read() == b"upstream:before-deadline"
            with pytest.raises(grpc.aio.AioRpcError) as expired:
                await deadline.read()
            assert expired.value.code() is grpc.StatusCode.DEADLINE_EXCEEDED
            async with asyncio.timeout(5):
                await finished.wait()
            finished.clear()
            cancelled = channel.stream_stream(
                _CONTRACT_PATH, request_serializer=lambda value: value, response_deserializer=lambda value: value
            )(metadata=(("x-buildbuddy-api-key", placeholder_of(_CONTRACT_CREDENTIAL)),), timeout=10)
            await cancelled.write(b"before-cancel")
            assert await cancelled.read() == b"upstream:before-cancel"
            assert cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled.read()
            async with asyncio.timeout(5):
                await finished.wait()
    headers = dict(await requests.get())
    assert headers["x-buildbuddy-api-key"] == "contract-secret"
    assert "proxy-authorization" not in headers


if __name__ == "__main__":
    pytest_bazel.main()

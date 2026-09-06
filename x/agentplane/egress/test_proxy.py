"""End to end: a real client through the hosted mitmproxy to a scripted HTTPS upstream.

The proxy intercepts with a throwaway CA the client trusts, verifies the upstream's certificate
against a second throwaway CA, takes its identity from the fake API server, and its policy from the
informer over the same fake. Every assertion is on what the client and the upstream actually saw.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path

import aiohttp
import grpc
import pytest
import pytest_bazel
from aiohttp import web
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api
from mitmproxy import connection, http
from more_itertools import one

from x.agentplane.egress.addon import DENIED_HEADER, EgressAddon
from x.agentplane.egress.admin import create_admin_app, serve_admin
from x.agentplane.egress.conftest import (
    AUDIENCE,
    GITHUB_POLICY,
    PLACEHOLDER,
    POD_A_IP,
    POD_B_UID,
    SANDBOX_A,
    SANDBOX_B,
    SCHEME,
    SECRET_NAME,
    SECRET_VALUE,
    TOKEN_A,
    TOKEN_B,
    UPSTREAM_HOST,
    informer,
)
from x.agentplane.egress.decisions import DecisionRing
from x.agentplane.egress.identity import IdentityRejectedError, PodIdentityVerifier
from x.agentplane.egress.policy import DenyReason, Index
from x.agentplane.egress.proxy import EgressProxyServer, write_interception_ca
from x.agentplane.egress.resources import TargetMethod, placeholder_of
from x.agentplane.egress.rules_api import HOST as RULES_HOST, PATH as RULES_PATH, RulesApi, RulesProjection
from x.agentplane.egress.sidecar import SidecarRelay
from x.agentplane.egress.testing.fake_apiserver import (
    BINDINGS_PLURAL,
    CREDENTIALS_PLURAL,
    POLICIES_PLURAL,
    SANDBOX_NAMESPACE,
    SANDBOXES_PLURAL,
    SECRETS_PLURAL,
    FakeApiServer,
    TokenVerdict,
    authenticated_workload_credential,
    binding,
    credential,
    pod_for,
    policy,
    sandbox,
    secret,
)
from x.agentplane.egress.testing.tls import (
    CertificateAuthority,
    client_tls_context,
    issue_leaf,
    make_ca,
    server_tls_context,
    write_ca,
)
from x.agentplane.egress.upstream import Network, UpstreamResolver


@dataclass
class RecordingUpstream:
    """HTTP and HTTPS listeners that record every request's method, path, and headers."""

    port: int = 0
    http_port: int = 0
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.requests.append((request.method, request.path_qs, headers))
        if request.path == "/workload/requires-auth" and "authorization" not in headers:
            return web.Response(status=401, text="authentication required")
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


@dataclass(frozen=True)
class Response:
    """What the client saw, read while the session that fetched it is still open.

    Reading here is the point, not a convenience. Handing back aiohttp's own response would outlive
    its session, and closing a session whose body is still unread aborts the connection instead of
    ending it -- which leaves mitmproxy holding a flow whose client vanished mid-stream, and its
    shutdown then never completes.
    """

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass
class ProxyUnderTest:
    proxy_port: int
    admin_port: int
    interception_ca: CertificateAuthority
    upstream_ca: CertificateAuthority
    tmp_path: Path
    index: Index
    upstream: RecordingUpstream
    addon: EgressAddon

    def url(self, path: str, *, tls: bool = True) -> str:
        scheme = "https" if tls else "http"
        port = self.upstream.port if tls else self.upstream.http_port
        return f"{scheme}://{UPSTREAM_HOST}:{port}{path}"

    async def get_rules(
        self,
        path: str,
        *,
        token: str | None = TOKEN_A,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        proxy_port: int | None = None,
    ) -> Response:
        """A request to the Service DNS rules name, locally answered by the central proxy."""
        async with (
            aiohttp.ClientSession() as session,
            session.request(
                "GET",
                f"https://{RULES_HOST}{path}",
                proxy=f"http://127.0.0.1:{proxy_port or self.proxy_port}",
                proxy_headers={"Proxy-Authorization": f"Bearer {token}"} if token is not None else None,
                ssl=client_tls_context(self.interception_ca),
                headers=headers,
                data=body,
            ) as response,
        ):
            return Response(status=response.status, headers=dict(response.headers), body=await response.read())

    async def get(self, path: str, *, token: str | None = TOKEN_A, headers: dict[str, str] | None = None) -> Response:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                self.url(path),
                proxy=f"http://127.0.0.1:{self.proxy_port}",
                proxy_headers={"Proxy-Authorization": f"Bearer {token}"} if token is not None else None,
                ssl=client_tls_context(self.interception_ca),
                headers=headers,
            ) as response,
        ):
            return Response(status=response.status, headers=dict(response.headers), body=await response.read())


@pytest.fixture
def exempt_networks() -> frozenset[Network]:
    """The scripted upstream listens on loopback, which the proxy refuses unless told otherwise."""
    return frozenset({ip_network("127.0.0.0/8"), ip_network("::1/128")})


@pytest.fixture
async def proxy(
    fake: FakeApiServer, api_client: ApiClient, tmp_path: Path, exempt_networks: frozenset[Network]
) -> AsyncIterator[ProxyUnderTest]:
    interception_ca = make_ca("agentplane-egress-test-interception")
    write_interception_ca(tmp_path / "confdir", *write_ca(interception_ca, tmp_path, "interception"))
    upstream_ca = make_ca("agentplane-egress-test-upstream")
    upstream_ca_cert, _ = write_ca(upstream_ca, tmp_path, "upstream")
    index = Index()
    ring = DecisionRing(capacity=200)
    verifier = PodIdentityVerifier(
        authentication=AuthenticationV1Api(api_client),
        core_v1=CoreV1Api(api_client),
        namespace=SANDBOX_NAMESPACE,
        audience=AUDIENCE,
        cache_seconds=60,
    )
    informer_task = asyncio.create_task(informer(index, api_client).run())
    try:
        await index.wait_for(lambda: index.synced)
        async with (
            recording_upstream(*issue_leaf(upstream_ca, UPSTREAM_HOST, tmp_path)) as upstream,
            EgressProxyServer(
                addon := EgressAddon(
                    index=index,
                    verifier=verifier,
                    ring=ring,
                    resolver=UpstreamResolver(exempt=exempt_networks),
                    rules_api=RulesApi(RulesProjection(index)),
                ),
                confdir=tmp_path / "confdir",
                extra_options={"ssl_verify_upstream_trusted_ca": str(upstream_ca_cert)},
            ) as server,
            serve_admin(create_admin_app(ring, index, resync_seconds=300), "127.0.0.1", 0) as admin_port,
        ):
            yield ProxyUnderTest(
                proxy_port=server.listen_port,
                admin_port=admin_port,
                interception_ca=interception_ca,
                upstream_ca=upstream_ca,
                tmp_path=tmp_path,
                index=index,
                upstream=upstream,
                addon=addon,
            )
    finally:
        informer_task.cancel()
        await asyncio.gather(informer_task, return_exceptions=True)


BINDING = f"{SANDBOX_A}-{GITHUB_POLICY}"
WORKLOAD_CREDENTIAL = "agentplane-workload"
WORKLOAD_PLACEHOLDER = placeholder_of(WORKLOAD_CREDENTIAL)
WORKLOAD_POLICY = "first-party-workload"


def denial(response: Response, reason: DenyReason) -> bool:
    return response.status == 403 and response.headers[DENIED_HEADER] == f"denied; reason={reason}"


def authentication_flow(client: connection.Client, proxy_authorization: str | None) -> http.HTTPFlow:
    flow = http.HTTPFlow(client, connection.Server(address=(UPSTREAM_HOST, 80)))
    headers = http.Headers()
    if proxy_authorization is not None:
        headers["Proxy-Authorization"] = proxy_authorization
    flow.request = http.Request.make("GET", f"http://{UPSTREAM_HOST}/workload/context", headers=headers)
    return flow


@pytest.mark.parametrize(
    ("replacement", "reason"),
    [("Basic malformed", DenyReason.TOKEN_MISSING), ("Bearer rejected", DenyReason.TOKEN_REJECTED)],
)
async def test_rejected_replacement_clears_authenticated_connection_context(
    proxy: ProxyUnderTest, replacement: str, reason: DenyReason
) -> None:
    client = connection.Client(peername=(POD_A_IP, 12345), sockname=("127.0.0.1", 8080))
    admitted = authentication_flow(client, f"Bearer {TOKEN_A}")
    assert (await proxy.addon._sandbox_of(admitted)).metadata.name == SANDBOX_A
    assert "proxy-authorization" not in admitted.request.headers

    rejected = authentication_flow(client, replacement)
    with pytest.raises(IdentityRejectedError) as refusal:
        await proxy.addon._sandbox_of(rejected)
    assert refusal.value.reason is reason
    assert "proxy-authorization" not in rejected.request.headers

    with pytest.raises(IdentityRejectedError) as after:
        await proxy.addon._sandbox_of(authentication_flow(client, None))
    assert after.value.reason is DenyReason.TOKEN_MISSING


async def test_connection_end_clears_authenticated_context(proxy: ProxyUnderTest) -> None:
    client = connection.Client(peername=(POD_A_IP, 12345), sockname=("127.0.0.1", 8080))
    assert (await proxy.addon._sandbox_of(authentication_flow(client, f"Bearer {TOKEN_A}"))).metadata.name == SANDBOX_A

    proxy.addon.client_disconnected(client)

    with pytest.raises(IdentityRejectedError) as after:
        await proxy.addon._sandbox_of(authentication_flow(client, None))
    assert after.value.reason is DenyReason.TOKEN_MISSING


async def install_workload_credential(fake: FakeApiServer, proxy: ProxyUnderTest, *, bind_b: bool = False) -> None:
    fake.put(
        CREDENTIALS_PLURAL,
        authenticated_workload_credential(
            WORKLOAD_CREDENTIAL,
            targets=[{"header": "Authorization", "method": TargetMethod.SCHEME_TOKEN, "scheme": "Bearer"}],
        ),
    )
    fake.put(
        POLICIES_PLURAL,
        policy(
            WORKLOAD_POLICY,
            [
                {
                    "hosts": [UPSTREAM_HOST],
                    "methods": ["GET"],
                    "paths": ["/workload/**"],
                    "credentialRef": {"name": WORKLOAD_CREDENTIAL},
                }
            ],
        ),
    )
    fake.put(
        BINDINGS_PLURAL,
        binding(BINDING, subjects=[{"sandbox": {"name": SANDBOX_A}}], policies=[GITHUB_POLICY, WORKLOAD_POLICY]),
    )
    if bind_b:
        # A faithful second Pod identity reaching the in-process listener from the same loopback
        # address. Its distinct Pod UID and token still go through TokenReview and live ownership.
        fake.pods[SANDBOX_B] = pod_for(fake, SANDBOX_B, pod_uid=POD_B_UID, ip=POD_A_IP)
        fake.put(
            BINDINGS_PLURAL,
            binding(
                f"{SANDBOX_B}-{WORKLOAD_POLICY}",
                subjects=[{"sandbox": {"name": SANDBOX_B}}],
                policies=[WORKLOAD_POLICY],
            ),
        )
    await proxy.index.wait_for(
        lambda: (
            WORKLOAD_CREDENTIAL in proxy.index.credentials
            and WORKLOAD_POLICY in proxy.index.policies
            and WORKLOAD_POLICY in proxy.index.bindings[BINDING].spec.policies
            and (not bind_b or f"{SANDBOX_B}-{WORKLOAD_POLICY}" in proxy.index.bindings)
        )
    )


async def test_allowed_request_has_its_placeholder_substituted(proxy: ProxyUnderTest) -> None:
    response = await proxy.get("/repos/o/r?ref=main", headers={"Authorization": f"Bearer {PLACEHOLDER}"})
    assert (response.status, response.body) == (200, b"upstream ok")
    method, path, headers = one(proxy.upstream.requests)
    assert (method, path) == ("GET", "/repos/o/r?ref=main")
    assert headers["authorization"] == f"Bearer {SECRET_VALUE}"
    assert "proxy-authorization" not in headers


async def test_authenticated_workload_source_uses_each_https_tunnels_own_token(
    fake: FakeApiServer, proxy: ProxyUnderTest
) -> None:
    await install_workload_credential(fake, proxy, bind_b=True)

    for token, suffix in ((TOKEN_A, "a"), (TOKEN_B, "b")):
        token_file = proxy.tmp_path / f"workload-sidecar-token-{suffix}"
        token_file.write_text(token)
        async with (
            SidecarRelay(
                proxy_host="127.0.0.1", proxy_port=proxy.proxy_port, token_file=token_file, listen_port=0
            ) as sidecar,
            aiohttp.ClientSession() as session,
            session.get(
                proxy.url(f"/workload/{suffix}"),
                proxy=f"http://127.0.0.1:{sidecar.listen_port}",
                ssl=client_tls_context(proxy.interception_ca),
                headers={"Authorization": f"Bearer {WORKLOAD_PLACEHOLDER}"},
            ) as response,
        ):
            assert (response.status, await response.read()) == (200, b"upstream ok")

    assert [headers["authorization"] for _, _, headers in proxy.upstream.requests] == [
        f"Bearer {TOKEN_A}",
        f"Bearer {TOKEN_B}",
    ]
    assert all("proxy-authorization" not in headers for _, _, headers in proxy.upstream.requests)

    view = await proxy.get_rules(RULES_PATH)
    assert view.status == 200
    assert WORKLOAD_PLACEHOLDER in view.body.decode()
    assert all(token not in view.body.decode() for token in (TOKEN_A, TOKEN_B))

    async with (
        aiohttp.ClientSession(f"http://127.0.0.1:{proxy.admin_port}") as admin,
        admin.get("/decisions") as listing,
    ):
        decisions = await listing.text()
    assert all(value not in decisions for value in (TOKEN_A, TOKEN_B, WORKLOAD_PLACEHOLDER))


async def test_authenticated_workload_source_uses_validated_context_for_plain_http(
    fake: FakeApiServer, proxy: ProxyUnderTest
) -> None:
    await install_workload_credential(fake, proxy)
    token_file = proxy.tmp_path / "plain-http-sidecar-token"
    token_file.write_text(TOKEN_A)

    async with (
        SidecarRelay(
            proxy_host="127.0.0.1", proxy_port=proxy.proxy_port, token_file=token_file, listen_port=0
        ) as sidecar,
        aiohttp.ClientSession() as session,
        session.get(
            proxy.url("/workload/http", tls=False),
            proxy=f"http://127.0.0.1:{sidecar.listen_port}",
            headers={"Authorization": f"Bearer {WORKLOAD_PLACEHOLDER}"},
        ) as response,
    ):
        assert (response.status, await response.read()) == (200, b"upstream ok")

    _, _, headers = one(proxy.upstream.requests)
    assert headers["authorization"] == f"Bearer {TOKEN_A}"
    assert "proxy-authorization" not in headers


async def test_authenticated_workload_source_does_not_apply_to_the_wrong_target(
    fake: FakeApiServer, proxy: ProxyUnderTest
) -> None:
    await install_workload_credential(fake, proxy)

    response = await proxy.get("/workload/requires-auth", headers={"X-Workload-Credential": WORKLOAD_PLACEHOLDER})

    assert (response.status, response.body) == (401, b"authentication required")
    _, _, headers = one(proxy.upstream.requests)
    assert "authorization" not in headers
    assert headers["x-workload-credential"] == WORKLOAD_PLACEHOLDER


async def test_forged_proxy_authorization_never_becomes_dynamic_credential(
    fake: FakeApiServer, proxy: ProxyUnderTest, caplog: pytest.LogCaptureFixture
) -> None:
    await install_workload_credential(fake, proxy)
    forged = "forged-workload-bearer-value"
    caplog.set_level(logging.INFO, logger="x.agentplane.egress.addon")

    with pytest.raises(aiohttp.ClientHttpProxyError) as refused:
        await proxy.get("/workload/forged", token=forged, headers={"Authorization": f"Bearer {WORKLOAD_PLACEHOLDER}"})

    assert refused.value.status == 403
    assert refused.value.headers is not None
    assert refused.value.headers[DENIED_HEADER] == f"denied; reason={DenyReason.TOKEN_REJECTED}"
    assert proxy.upstream.requests == []
    assert forged not in caplog.text


@asynccontextmanager
async def recording_grpc_upstream(
    cert_pem: bytes, key_pem: bytes
) -> AsyncIterator[tuple[int, asyncio.Queue[tuple[tuple[str, str], ...]]]]:
    """A TLS gRPC server recording metadata on unary and bidirectional calls."""
    requests: asyncio.Queue[tuple[tuple[str, str], ...]] = asyncio.Queue()

    async def call(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        del request
        metadata = context.invocation_metadata() or ()
        await requests.put(tuple((str(item[0]), str(item[1])) for item in metadata))
        return b"upstream ok"

    async def stream(request_iterator: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
        metadata = context.invocation_metadata() or ()
        await requests.put(tuple((str(item[0]), str(item[1])) for item in metadata))
        async for request in request_iterator:
            yield b"upstream:" + request
        context.set_trailing_metadata((("buildbuddy-test-trailer", "ok"),))

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "build.bazel.remote.execution.v2.Capabilities",
                {
                    "GetCapabilities": grpc.unary_unary_rpc_method_handler(
                        call, request_deserializer=lambda value: value, response_serializer=lambda value: value
                    )
                },
            ),
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
    port = server.add_secure_port("127.0.0.1:0", grpc.ssl_server_credentials([(key_pem, cert_pem)]))
    await server.start()
    try:
        yield port, requests
    finally:
        await server.stop(grace=None)


def read_keypair(cert_path: Path, key_path: Path) -> tuple[bytes, bytes]:
    return cert_path.read_bytes(), key_path.read_bytes()


async def test_buildbuddy_http_and_grpc_metadata_placeholder_is_substituted(
    fake: FakeApiServer, proxy: ProxyUnderTest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bazel's remote_header becomes ordinary HTTP/2 metadata at the intercepted proxy."""
    credential_name = "buildbuddy-api-key"
    credential_secret = "buildbuddy-api-key-secret"
    policy_name = "buildbuddy-local-bazel"
    placeholder = placeholder_of(credential_name)
    fake.put(SECRETS_PLURAL, secret(credential_secret, {"api-key": "buildbuddy-secret"}))
    fake.put(
        CREDENTIALS_PLURAL,
        credential(
            credential_name,
            secret_name=credential_secret,
            key="api-key",
            targets=[{"header": "x-buildbuddy-api-key", "method": TargetMethod.WHOLE_VALUE}],
        ),
    )
    fake.put(
        POLICIES_PLURAL,
        policy(
            policy_name,
            [
                {
                    "hosts": [UPSTREAM_HOST],
                    "methods": ["POST"],
                    "paths": [
                        "/api/v1/GetInvocation",
                        "/build.bazel.remote.execution.v2.Capabilities/GetCapabilities",
                        "/google.devtools.build.v1.PublishBuildEvent/PublishBuildToolEventStream",
                    ],
                    "credentialRef": {"name": credential_name},
                }
            ],
        ),
    )
    fake.put(
        BINDINGS_PLURAL,
        binding(BINDING, subjects=[{"sandbox": {"name": SANDBOX_A}}], policies=[GITHUB_POLICY, policy_name]),
    )
    await proxy.index.wait_for(
        lambda: (
            credential_name in proxy.index.credentials
            and policy_name in proxy.index.policies
            and policy_name in proxy.index.bindings[BINDING].spec.policies
        )
    )

    async with (
        aiohttp.ClientSession() as session,
        session.post(
            proxy.url("/api/v1/GetInvocation"),
            proxy=f"http://127.0.0.1:{proxy.proxy_port}",
            proxy_headers={"Proxy-Authorization": f"Bearer {TOKEN_A}"},
            ssl=client_tls_context(proxy.interception_ca),
            headers={"x-buildbuddy-api-key": placeholder},
            json={"selector": {"invocation_id": "fake"}},
        ) as http_response,
    ):
        assert (http_response.status, await http_response.text()) == (200, "upstream ok")
    assert one(proxy.upstream.requests)[2]["x-buildbuddy-api-key"] == "buildbuddy-secret"

    token_file = proxy.tmp_path / "sidecar-token"
    token_file.write_text(TOKEN_A)
    cert_path, key_path = issue_leaf(proxy.upstream_ca, UPSTREAM_HOST, proxy.tmp_path)
    cert_pem, key_pem = read_keypair(cert_path, key_path)
    async with (
        recording_grpc_upstream(cert_pem, key_pem) as (port, requests),
        SidecarRelay(
            proxy_host="127.0.0.1", proxy_port=proxy.proxy_port, token_file=token_file, listen_port=0
        ) as sidecar,
    ):
        monkeypatch.setenv("https_proxy", f"http://127.0.0.1:{sidecar.listen_port}")
        monkeypatch.setenv("no_proxy", "")
        credentials = grpc.ssl_channel_credentials(root_certificates=proxy.interception_ca.cert_pem)
        async with grpc.aio.secure_channel(f"{UPSTREAM_HOST}:{port}", credentials) as channel:
            response = await channel.unary_unary(
                "/build.bazel.remote.execution.v2.Capabilities/GetCapabilities",
                request_serializer=lambda value: value,
                response_deserializer=lambda value: value,
            )(b"request", metadata=(("x-buildbuddy-api-key", placeholder),), timeout=10)
            stream = channel.stream_stream(
                "/google.devtools.build.v1.PublishBuildEvent/PublishBuildToolEventStream",
                request_serializer=lambda value: value,
                response_deserializer=lambda value: value,
            )(metadata=(("x-buildbuddy-api-key", placeholder),), timeout=10)
            await stream.write(b"one")
            await stream.write(b"two")
            await stream.done_writing()
            streamed = [message async for message in stream]
            trailers = dict(await stream.trailing_metadata() or ())

    assert response == b"upstream ok"
    assert streamed == [b"upstream:one", b"upstream:two"]
    assert trailers["buildbuddy-test-trailer"] == "ok"
    assert [dict(await requests.get())["x-buildbuddy-api-key"] for _ in range(2)] == [
        "buildbuddy-secret",
        "buildbuddy-secret",
    ]


async def test_allowed_request_without_placeholder_is_forwarded_as_is(proxy: ProxyUnderTest) -> None:
    response = await proxy.get("/public/readme")
    assert response.status == 200
    _, _, headers = one(proxy.upstream.requests)
    assert "authorization" not in headers


async def test_inner_request_denied_by_rule_inside_an_admitted_tunnel(proxy: ProxyUnderTest) -> None:
    response = await proxy.get("/private/x")
    assert denial(response, DenyReason.NO_RULE)
    assert response.body == b""
    assert proxy.upstream.requests == []


async def test_unresolved_placeholder_is_never_forwarded(proxy: ProxyUnderTest) -> None:
    response = await proxy.get("/public/readme", headers={"Authorization": f"Bearer {PLACEHOLDER}"})
    assert denial(response, DenyReason.PLACEHOLDER_UNRESOLVED)
    assert proxy.upstream.requests == []


async def test_bad_token_refused_at_connect(proxy: ProxyUnderTest) -> None:
    with pytest.raises(aiohttp.ClientHttpProxyError) as refused:
        await proxy.get("/repos/o/r", token="not-a-token")
    assert refused.value.status == 403
    assert refused.value.headers is not None
    assert refused.value.headers[DENIED_HEADER] == f"denied; reason={DenyReason.TOKEN_REJECTED}"
    assert proxy.upstream.requests == []


async def test_missing_token_refused_at_connect(proxy: ProxyUnderTest) -> None:
    with pytest.raises(aiohttp.ClientHttpProxyError) as refused:
        await proxy.get("/repos/o/r", token=None)
    assert refused.value.status == 403
    assert refused.value.headers is not None
    assert refused.value.headers[DENIED_HEADER] == f"denied; reason={DenyReason.TOKEN_MISSING}"


async def test_copied_token_refused_at_connect(proxy: ProxyUnderTest) -> None:
    """Pod B's token, presented from an address that is not Pod B's."""
    with pytest.raises(aiohttp.ClientHttpProxyError) as refused:
        await proxy.get("/repos/o/r", token=TOKEN_B)
    assert refused.value.status == 403
    assert refused.value.headers is not None
    assert refused.value.headers[DENIED_HEADER] == f"denied; reason={DenyReason.POD_MISMATCH}"


async def test_unbound_sandbox_refused(fake: FakeApiServer, proxy: ProxyUnderTest) -> None:
    fake.put(SANDBOXES_PLURAL, sandbox("sb-c"))
    fake.pods["sb-c"] = pod_for(fake, "sb-c", pod_uid="pod-c-uid", ip=POD_A_IP)
    fake.tokens["token-c"] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:sandbox",
        pod_name="sb-c",
        pod_uid="pod-c-uid",
        audiences=(AUDIENCE,),
    )
    await proxy.index.wait_for(lambda: "sb-c" in proxy.index.sandboxes)
    with pytest.raises(aiohttp.ClientHttpProxyError) as refused:
        await proxy.get("/repos/o/r", token="token-c")
    assert refused.value.headers is not None
    assert refused.value.headers[DENIED_HEADER] == f"denied; reason={DenyReason.NO_BINDING}"


class TestUpstreamAddress:
    @pytest.fixture
    def exempt_networks(self) -> frozenset[Network]:
        return frozenset()

    async def test_host_resolving_into_a_private_range_is_refused_at_connect(self, proxy: ProxyUnderTest) -> None:
        """The policy admits the host by name; the name points at loopback, so the tunnel is refused."""
        with pytest.raises(aiohttp.ClientHttpProxyError) as refused:
            await proxy.get("/repos/o/r")
        assert refused.value.status == 403
        assert refused.value.headers is not None
        assert refused.value.headers[DENIED_HEADER] == f"denied; reason={DenyReason.ADDRESS_FORBIDDEN}"
        assert proxy.upstream.requests == []


async def test_rotated_secret_is_substituted_without_restart(fake: FakeApiServer, proxy: ProxyUnderTest) -> None:
    fake.put(SECRETS_PLURAL, secret(SECRET_NAME, {"token": "real-secret-v2"}))
    await proxy.index.wait_for(lambda: proxy.index.secrets[SECRET_NAME].data["token"] == "real-secret-v2")
    response = await proxy.get("/repos/o/r", headers={"Authorization": f"Bearer {PLACEHOLDER}"})
    assert response.status == 200
    assert one(proxy.upstream.requests)[2]["authorization"] == "Bearer real-secret-v2"


async def test_admin_serves_decisions_and_health(proxy: ProxyUnderTest) -> None:
    await proxy.get("/repos/o/r", headers={"Authorization": f"Bearer {PLACEHOLDER}"})
    await proxy.get("/private/x")
    with pytest.raises(aiohttp.ClientHttpProxyError):
        await proxy.get("/repos/o/r", token="not-a-token")
    async with aiohttp.ClientSession(f"http://127.0.0.1:{proxy.admin_port}") as admin:
        async with admin.get("/healthz") as health:
            assert (health.status, (await health.json())["synced"]) == (200, True)
        async with admin.get("/decisions", params={"sandbox": SANDBOX_A}) as listing:
            decisions = await listing.json()
        async with admin.get("/decisions") as listing:
            unidentified = await listing.json()
    assert [(d["method"], d["path"], d["outcome"], d["reason"], d["substituted"]) for d in decisions] == [
        ("CONNECT", None, "allow", None, False),
        ("GET", "/repos/o/r", "allow", None, True),
        ("CONNECT", None, "allow", None, False),
        ("GET", "/private/x", "deny", "no-rule", False),
    ]
    assert [d["address"] for d in decisions] == ["127.0.0.1", "127.0.0.1", "127.0.0.1", None]
    substituted = one(d for d in decisions if d["substituted"])
    assert (substituted["binding"], substituted["policy"]) == (BINDING, GITHUB_POLICY)
    assert [(d["method"], d["reason"]) for d in unidentified] == [("CONNECT", "token-rejected")]
    assert all(SECRET_VALUE not in str(d) and PLACEHOLDER not in str(d) for d in decisions)


if __name__ == "__main__":
    pytest_bazel.main()


async def test_a_sandbox_reads_the_rules_that_apply_to_it(proxy: ProxyUnderTest) -> None:
    """The Service DNS request is locally dispatched under the existing hop identity, never dialled."""
    response = await proxy.get_rules(RULES_PATH)

    assert response.status == 200, response.body
    assert proxy.upstream.requests == [], "the central proxy forwarded its locally served rules route"
    view = json.loads(response.body)
    assert view["sandbox"] == SANDBOX_A
    credentials = [rule["credential"] for policy in view["policies"] for rule in policy["rules"]]
    presented = one(c for c in credentials if c is not None)
    assert presented["placeholder"] == PLACEHOLDER, view
    # The targets, not the placeholder alone: without the scheme a sandbox sends the placeholder
    # bare and the upstream refuses the substituted value.
    assert {"header": "Authorization", "method": "schemeToken", "scheme": SCHEME} in presented["targets"], view
    assert SECRET_VALUE not in response.body.decode(), "the proxy handed the sandbox the real credential"


async def test_a_sandbox_reads_rules_through_its_loopback_sidecar(proxy: ProxyUnderTest) -> None:
    """The deployed path: ordinary HTTPS proxying to loopback, then the authenticated central hop."""
    token_file = proxy.tmp_path / "rules-sidecar-token"
    token_file.write_text(TOKEN_A)
    async with SidecarRelay(
        proxy_host="127.0.0.1", proxy_port=proxy.proxy_port, token_file=token_file, listen_port=0
    ) as sidecar:
        response = await proxy.get_rules(RULES_PATH, token=None, proxy_port=sidecar.listen_port)

    assert response.status == 200, response.body
    assert json.loads(response.body)["sandbox"] == SANDBOX_A


async def test_rules_identity_ignores_forged_request_headers_and_body(proxy: ProxyUnderTest) -> None:
    response = await proxy.get_rules(
        RULES_PATH,
        headers={"X-Agentplane-Sandbox": SANDBOX_B, "Content-Type": "application/json"},
        body=json.dumps({"sandbox": SANDBOX_B, "sandbox_uid": POD_B_UID}).encode(),
    )

    assert response.status == 200, response.body
    assert json.loads(response.body)["sandbox"] == SANDBOX_A


async def test_the_agent_view_needs_the_same_identity_every_request_does(proxy: ProxyUnderTest) -> None:
    """Refused at the CONNECT, before a handshake it would learn nothing from."""
    with pytest.raises(aiohttp.ClientHttpProxyError) as refused:
        await proxy.get_rules(RULES_PATH, token=None)

    assert refused.value.status == 403
    assert refused.value.headers is not None
    assert refused.value.headers[DENIED_HEADER] == f"denied; reason={DenyReason.TOKEN_MISSING}"


async def test_the_proxys_own_service_name_serves_nothing_else(proxy: ProxyUnderTest) -> None:
    """One local path, so the proxy Service name cannot become an accidental surface."""
    response = await proxy.get_rules("/")

    assert response.status == 404, response.body

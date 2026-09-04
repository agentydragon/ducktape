"""End to end: a real client through the hosted mitmproxy to a scripted HTTPS upstream.

The proxy intercepts with a throwaway CA the client trusts, verifies the upstream's certificate
against a second throwaway CA, takes its identity from the fake API server, and its policy from the
informer over the same fake. Every assertion is on what the client and the upstream actually saw.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel
from aiohttp import web
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api
from more_itertools import one

from x.agentplane.egress.addon import DENIED_HEADER, RULES_PATH, SELF_HOST, EgressAddon
from x.agentplane.egress.admin import create_admin_app, serve_admin
from x.agentplane.egress.conftest import (
    AUDIENCE,
    GITHUB_POLICY,
    PLACEHOLDER,
    POD_A_IP,
    SANDBOX_A,
    SCHEME,
    SECRET_NAME,
    SECRET_VALUE,
    TOKEN_A,
    TOKEN_B,
    UPSTREAM_HOST,
    informer,
)
from x.agentplane.egress.decisions import DecisionRing
from x.agentplane.egress.identity import PodIdentityVerifier
from x.agentplane.egress.policy import DenyReason, Index
from x.agentplane.egress.proxy import EgressProxyServer, write_interception_ca
from x.agentplane.egress.testing.fake_apiserver import (
    SANDBOX_NAMESPACE,
    SANDBOXES_PLURAL,
    SECRETS_PLURAL,
    FakeApiServer,
    TokenVerdict,
    pod_for,
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
    """An HTTPS upstream that records every request's method, path, and headers."""

    port: int = 0
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append((request.method, request.path_qs, {k.lower(): v for k, v in request.headers.items()}))
        return web.Response(text="upstream ok")


@asynccontextmanager
async def recording_upstream(cert_path: Path, key_path: Path) -> AsyncIterator[RecordingUpstream]:
    upstream = RecordingUpstream()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", upstream.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls_context(cert_path, key_path))
    await site.start()
    upstream.port = one(runner.addresses)[1]
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
    index: Index
    upstream: RecordingUpstream

    def url(self, path: str) -> str:
        return f"https://{UPSTREAM_HOST}:{self.upstream.port}{path}"

    async def get_self(self, path: str, *, token: str | None = TOKEN_A) -> Response:
        """A request to the proxy's own name: tunnelled like any other, answered without an upstream."""
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"https://{SELF_HOST}{path}",
                proxy=f"http://127.0.0.1:{self.proxy_port}",
                proxy_headers={"Proxy-Authorization": f"Bearer {token}"} if token is not None else None,
                ssl=client_tls_context(self.interception_ca),
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
                EgressAddon(
                    index=index, verifier=verifier, ring=ring, resolver=UpstreamResolver(exempt=exempt_networks)
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
                index=index,
                upstream=upstream,
            )
    finally:
        informer_task.cancel()
        await asyncio.gather(informer_task, return_exceptions=True)


BINDING = f"{SANDBOX_A}-{GITHUB_POLICY}"


def denial(response: Response, reason: DenyReason) -> bool:
    return response.status == 403 and response.headers[DENIED_HEADER] == f"denied; reason={reason}"


async def test_allowed_request_has_its_placeholder_substituted(proxy: ProxyUnderTest) -> None:
    response = await proxy.get("/repos/o/r?ref=main", headers={"Authorization": f"Bearer {PLACEHOLDER}"})
    assert (response.status, response.body) == (200, b"upstream ok")
    method, path, headers = one(proxy.upstream.requests)
    assert (method, path) == ("GET", "/repos/o/r?ref=main")
    assert headers["authorization"] == f"Bearer {SECRET_VALUE}"
    assert "proxy-authorization" not in headers


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
    """C11: the placeholder a sandbox must present is knowable from inside the sandbox, over the one
    listener it can reach, under the identity it already proves for every request."""
    response = await proxy.get_self(RULES_PATH)

    assert response.status == 200, response.body
    view = json.loads(response.body)
    assert view["sandbox"] == SANDBOX_A
    credentials = [rule["credential"] for policy in view["policies"] for rule in policy["rules"]]
    presented = one(c for c in credentials if c is not None)
    assert presented["placeholder"] == PLACEHOLDER, view
    # The targets, not the placeholder alone: without the scheme a sandbox sends the placeholder
    # bare and the upstream refuses the substituted value.
    assert {"header": "Authorization", "method": "schemeToken", "scheme": SCHEME} in presented["targets"], view
    assert SECRET_VALUE not in response.body.decode(), "the proxy handed the sandbox the real credential"


async def test_the_agent_view_needs_the_same_identity_every_request_does(proxy: ProxyUnderTest) -> None:
    """Refused at the CONNECT, before a handshake it would learn nothing from."""
    with pytest.raises(aiohttp.ClientHttpProxyError) as refused:
        await proxy.get_self(RULES_PATH, token=None)

    assert refused.value.status == 403
    assert refused.value.headers is not None
    assert refused.value.headers[DENIED_HEADER] == f"denied; reason={DenyReason.TOKEN_MISSING}"


async def test_the_proxys_own_name_serves_nothing_else(proxy: ProxyUnderTest) -> None:
    """One path, so the reserved name cannot become an accidental surface."""
    response = await proxy.get_self("/")

    assert response.status == 404, response.body

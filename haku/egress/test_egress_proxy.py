"""Fail-closed conformance tests for the embedded egress proxy.

Every test drives a real client through a real in-process mitmproxy toward a local
recording upstream. "Fail closed" is asserted as both halves: the client gets a
refusal AND the upstream sees no TCP connection at all.

Plain HTTP (absolute-form proxying) keeps TLS trust out of the setup; the CONNECT tests
cover the tunnel path without the MITM CA because refusal happens before any TLS.

``LocalhostDecideClient`` runs the same drills against ``StubConsole``, an in-process
decide endpoint speaking the real wire models.

Pinned-dial enforcement (#4670's DNS-rebinding property) is also asserted with the
plain-HTTP harness: the TCP-recording upstreams observe the dialed socket peer, and a
decoy listener on the address a fresh resolution would yield must stay silent. TLS
behavior is preserved by construction rather than asserted here: the gate rewrites only
``Server.address`` at ``server_connect`` time, while ``Server.sni`` and the Host header
keep the hostname (asserted below), so mitmproxy's upstream verification still checks
the real hostname against the real certificate.

The reusable harness (recording upstream, stub console, decide-client doubles, pinned
dial helpers) lives in ``proxy_test_harness.py``; the #4914 integration suites build on
the same module.
"""

from __future__ import annotations

import base64
from contextlib import aclosing
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel
from mitmproxy.connection import Client, Server
from mitmproxy.proxy.server_hooks import ServerConnectionHookData
from more_itertools import one

from haku.egress.addon import EgressGateAddon
from haku.egress.decision import DecideDenied, GrantScope, RequestMeta
from haku.egress.localhost_decide_client import DEFAULT_TIMEOUT_SECONDS
from haku.egress.proxy_test_harness import (
    BRIDGE_BEARER,
    FENCE_CREDENTIAL,
    PLACEHOLDER,
    REAL_CREDENTIAL,
    GarbageBody,
    Hang,
    HangingDecideClient,
    MalformedDecideClient,
    QueueResolver,
    RaisingDecideClient,
    RecordingUpstream,
    StubBehavior,
    Unconfigured,
    allow_with_substitution,
    make_proxy,
    pinned_and_decoy_upstreams,
    proxied_get,
    stub_client,
    stub_console,
    tunneled_get,
)
from haku.egress.static_decide_client import StaticDecideClient


async def test_allow_substitutes_presented_placeholder(upstream: RecordingUpstream, tmp_path: Path) -> None:
    decide = StaticDecideClient(allow_with_substitution())
    async with make_proxy(decide, tmp_path) as proxy:
        status, body = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/hello", headers={"Authorization": f"Bearer {PLACEHOLDER}"}
        )
    assert (status, body) == (200, "upstream ok")
    recorded = one(upstream.requests)
    assert recorded.method == "GET"
    assert recorded.path == "/hello"
    assert recorded.headers["authorization"] == f"Bearer {REAL_CREDENTIAL}"
    assert decide.requests == [
        RequestMeta(method="GET", scheme="http", host="127.0.0.1", port=upstream.port, path="/hello")
    ]


async def test_allow_substitutes_inside_basic_base64_payload(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """Git over HTTPS authenticates with ``Basic base64(user:token)``; the swap reaches inside the payload."""
    decide = StaticDecideClient(allow_with_substitution())
    presented = base64.b64encode(f"x-access-token:{PLACEHOLDER}".encode()).decode()
    async with make_proxy(decide, tmp_path) as proxy:
        status, _body = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/git", headers={"Authorization": f"Basic {presented}"}
        )
    assert status == 200
    substituted = base64.b64encode(f"x-access-token:{REAL_CREDENTIAL}".encode()).decode()
    assert one(upstream.requests).headers["authorization"] == f"Basic {substituted}"


async def test_allow_without_placeholder_forwards_credential_free(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """A request that never presents the placeholder receives no credential anywhere."""
    decide = StaticDecideClient(allow_with_substitution())
    async with make_proxy(decide, tmp_path) as proxy:
        status_bare, _ = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/bare")
        status_other, _ = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/other", headers={"Authorization": "Bearer something-else"}
        )
    assert (status_bare, status_other) == (200, 200)
    bare, other = upstream.requests
    assert "authorization" not in bare.headers
    assert other.headers["authorization"] == "Bearer something-else"
    for recorded in (bare, other):
        assert REAL_CREDENTIAL not in recorded.path
        assert all(REAL_CREDENTIAL not in value for value in recorded.headers.values())


async def test_allow_passes_unscanned_placeholder_through_verbatim(upstream: RecordingUpstream, tmp_path: Path) -> None:
    """Only ``match_headers`` are scanned: elsewhere the inert placeholder rides along unsubstituted."""
    decide = StaticDecideClient(allow_with_substitution())
    async with make_proxy(decide, tmp_path) as proxy:
        status, _body = await proxied_get(
            proxy,
            f"http://127.0.0.1:{upstream.port}/lookup?q={PLACEHOLDER}",
            headers={"X-Unscanned": f"Bearer {PLACEHOLDER}"},
        )
    assert status == 200
    recorded = one(upstream.requests)
    assert recorded.path == f"/lookup?q={PLACEHOLDER}"
    assert recorded.headers["x-unscanned"] == f"Bearer {PLACEHOLDER}"
    assert REAL_CREDENTIAL not in recorded.path
    assert all(REAL_CREDENTIAL not in value for value in recorded.headers.values())


async def test_deny_refuses_without_upstream_contact(upstream: RecordingUpstream, tmp_path: Path) -> None:
    decide = StaticDecideClient(DecideDenied(reason="no standing policy or active grant"))
    async with make_proxy(decide, tmp_path) as proxy:
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 403
    assert "no standing policy or active grant" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_decide_exception_fails_closed(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with make_proxy(RaisingDecideClient(), tmp_path) as proxy:
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_decide_hang_fails_closed(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with make_proxy(HangingDecideClient(), tmp_path, decide_timeout_seconds=0.2) as proxy:
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_malformed_decision_fails_closed(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with make_proxy(MalformedDecideClient(), tmp_path) as proxy:
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_connect_deny_refuses_tunnel(upstream: RecordingUpstream, tmp_path: Path) -> None:
    decide = StaticDecideClient(DecideDenied(reason="no grant for origin"))
    async with make_proxy(decide, tmp_path) as proxy, aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientHttpProxyError) as excinfo:
            await session.get(f"https://127.0.0.1:{upstream.port}/", proxy=f"http://127.0.0.1:{proxy.listen_port}")
    assert excinfo.value.status == 403
    assert (upstream.connections, upstream.requests) == (0, [])
    assert decide.requests == [
        RequestMeta(method="CONNECT", scheme=None, host="127.0.0.1", port=upstream.port, path=None)
    ]


async def test_connect_decide_exception_fails_closed(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with make_proxy(RaisingDecideClient(), tmp_path) as proxy, aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientHttpProxyError) as excinfo:
            await session.get(f"https://127.0.0.1:{upstream.port}/", proxy=f"http://127.0.0.1:{proxy.listen_port}")
    assert excinfo.value.status == 502
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_localhost_decide_allow_flows_end_to_end(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with (
        stub_console(allow_with_substitution()) as stub,
        aclosing(stub_client(stub)) as decide,
        make_proxy(decide, tmp_path) as proxy,
    ):
        status, body = await proxied_get(
            proxy, f"http://127.0.0.1:{upstream.port}/hello", headers={"Authorization": f"Bearer {PLACEHOLDER}"}
        )
    assert (status, body) == (200, "upstream ok")
    assert one(upstream.requests).headers["authorization"] == f"Bearer {REAL_CREDENTIAL}"
    sent = one(stub.requests)
    assert sent.request == RequestMeta(method="GET", scheme="http", host="127.0.0.1", port=upstream.port, path="/hello")
    assert sent.proxy_client_credential is not None
    assert sent.proxy_client_credential.get_secret_value() == BRIDGE_BEARER
    assert "proxy-authorization" not in one(upstream.requests).headers
    assert (sent.resolved_ips, sent.upstream_ip) == (frozenset({IPv4Address("127.0.0.1")}), IPv4Address("127.0.0.1"))


async def test_invalid_proxy_client_bearer_is_refused_without_upstream_contact(
    upstream: RecordingUpstream, tmp_path: Path
) -> None:
    async with (
        make_proxy(StaticDecideClient(allow_with_substitution()), tmp_path) as proxy,
        aiohttp.ClientSession() as session,
        session.get(
            f"http://127.0.0.1:{upstream.port}/unauthenticated",
            proxy=f"http://127.0.0.1:{proxy.listen_port}",
            proxy_auth=aiohttp.BasicAuth("not-the-bearer", "wrong"),
        ) as response,
    ):
        status = response.status
    assert status == 407
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_localhost_decide_deny_refuses_without_upstream_contact(
    upstream: RecordingUpstream, tmp_path: Path
) -> None:
    denied = DecideDenied(
        reason="no standing policy or active grant",
        grant_scope=GrantScope(scheme="http", host="127.0.0.1", port=upstream.port),
    )
    async with (
        stub_console(denied) as stub,
        aclosing(stub_client(stub)) as decide,
        make_proxy(decide, tmp_path) as proxy,
    ):
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 403
    assert "no standing policy or active grant" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_localhost_decide_connect_deny_refuses_tunnel(upstream: RecordingUpstream, tmp_path: Path) -> None:
    async with (
        stub_console(DecideDenied(reason="no grant for origin")) as stub,
        aclosing(stub_client(stub)) as decide,
        make_proxy(decide, tmp_path) as proxy,
        aiohttp.ClientSession() as session,
    ):
        with pytest.raises(aiohttp.ClientHttpProxyError) as excinfo:
            await session.get(f"https://127.0.0.1:{upstream.port}/", proxy=f"http://127.0.0.1:{proxy.listen_port}")
    assert excinfo.value.status == 403
    assert (upstream.connections, upstream.requests) == (0, [])
    assert one(stub.requests).request == RequestMeta(
        method="CONNECT", scheme=None, host="127.0.0.1", port=upstream.port, path=None
    )


@dataclass(frozen=True)
class EndpointFailure:
    """One way the decide hop fails; each must refuse with zero upstream contact."""

    behavior: StubBehavior
    fence_credential: str = FENCE_CREDENTIAL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


ENDPOINT_FAILURES = [
    pytest.param(
        EndpointFailure(behavior=allow_with_substitution(), fence_credential="not-the-fence-credential"),
        id="rejected-fence-credential-401",
    ),
    pytest.param(EndpointFailure(behavior=Unconfigured()), id="unconfigured-503"),
    pytest.param(EndpointFailure(behavior=Hang(), timeout_seconds=0.2), id="endpoint-timeout"),
    pytest.param(EndpointFailure(behavior=GarbageBody()), id="garbage-body"),
]


@pytest.mark.parametrize("failure", ENDPOINT_FAILURES)
async def test_localhost_decide_endpoint_failure_fails_closed(
    failure: EndpointFailure, upstream: RecordingUpstream, tmp_path: Path
) -> None:
    async with (
        stub_console(failure.behavior) as stub,
        aclosing(
            stub_client(stub, fence_credential=failure.fence_credential, timeout_seconds=failure.timeout_seconds)
        ) as decide,
        make_proxy(decide, tmp_path) as proxy,
    ):
        status, body = await proxied_get(proxy, f"http://127.0.0.1:{upstream.port}/secret")
    assert status == 502
    assert "fail closed" in body
    assert (upstream.connections, upstream.requests) == (0, [])


async def test_pinned_dial_survives_rebinding_second_answer(tmp_path: Path) -> None:
    """The dial goes to the first validated answer even though a fresh resolution differs.

    End to end through ``LocalhostDecideClient``: the resolver's first answer (127.0.0.2) is
    validated by the decide exchange and pinned; its scripted second answer — and the system
    resolver's real answer for ``localhost`` — is 127.0.0.1, so a re-resolving dial would hit
    the decoy. Exactly one resolution may happen, and the decoy must stay silent.
    """
    resolver = QueueResolver(answers=[frozenset({IPv4Address("127.0.0.2")}), frozenset({IPv4Address("127.0.0.1")})])
    async with (
        pinned_and_decoy_upstreams() as (pinned, decoy),
        stub_console(allow_with_substitution()) as stub,
        aclosing(stub_client(stub)) as decide,
        make_proxy(decide, tmp_path, resolve=resolver) as proxy,
    ):
        status, body = await proxied_get(proxy, f"http://localhost:{pinned.port}/pinned")
    assert (status, body) == (200, "upstream ok")
    recorded = one(pinned.requests)
    assert recorded.path == "/pinned"
    # Host semantics preserved: the upstream sees the hostname, never the pinned literal.
    assert recorded.headers["host"] == f"localhost:{pinned.port}"
    assert (decoy.connections, decoy.requests) == (0, [])
    assert resolver.calls == 1
    sent = one(stub.requests)
    assert sent.request == RequestMeta(method="GET", scheme="http", host="localhost", port=pinned.port, path="/pinned")
    assert (sent.resolved_ips, sent.upstream_ip) == (frozenset({IPv4Address("127.0.0.2")}), IPv4Address("127.0.0.2"))


async def test_pinned_dial_connect_tunnel_and_inner_request(tmp_path: Path) -> None:
    """Both the tunnel admission and the inner request dial the validated address.

    The stub resolver answers 127.0.0.2 for the CONNECT decide and again for the inner
    request's decide; the system resolver's answer for ``localhost`` (127.0.0.1, the decoy)
    must never be dialed, and no third resolution may happen.
    """
    answer = frozenset({IPv4Address("127.0.0.2")})
    resolver = QueueResolver(answers=[answer, answer])
    decide = StaticDecideClient(allow_with_substitution())
    async with pinned_and_decoy_upstreams() as (pinned, decoy), make_proxy(decide, tmp_path, resolve=resolver) as proxy:
        connect_status, status, body = await tunneled_get(proxy.listen_port, f"localhost:{pinned.port}", "/tunneled")
    assert (connect_status, status, body) == (200, 200, "upstream ok")
    recorded = one(pinned.requests)
    assert (recorded.method, recorded.path) == ("GET", "/tunneled")
    assert recorded.headers["host"] == f"localhost:{pinned.port}"
    assert (decoy.connections, decoy.requests) == (0, [])
    assert resolver.calls == 2
    assert [request.method for request in decide.requests] == ["CONNECT", "GET"]
    assert all(request.host == "localhost" for request in decide.requests)


async def test_unpinned_upstream_dial_is_killed() -> None:
    """Defense in depth at the dial chokepoint: a ``server_connect`` no allow pinned is refused.

    Every gated flow pins its destination before mitmproxy dials, so this only triggers for a
    dial that never passed the gate — which must die rather than resolve.
    """
    addon = EgressGateAddon(StaticDecideClient(DecideDenied(reason="unreached")))
    server = Server(address=("evil.example", 443))
    addon.server_connect(
        ServerConnectionHookData(
            server=server, client=Client(peername=("127.0.0.1", 51234), sockname=("127.0.0.1", 8080))
        )
    )
    assert server.error is not None
    assert server.address == ("evil.example", 443)  # never rewritten toward a resolution


if __name__ == "__main__":
    pytest_bazel.main()

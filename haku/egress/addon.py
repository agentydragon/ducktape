"""Fail-closed gate: every request and CONNECT is refused unless a decide call allows it.

Stock mitmproxy swallows addon exceptions and lets the flow continue — an addon
crash FAILS OPEN. That is why the egress adapter embeds mitmproxy as a library
(github.com/agentydragon/ducktape/issues/4670) and why this addon never relies
on mitmproxy's error handling as the security boundary: each hook pre-sets a
refusal response and clears it only on an explicit allow, so a failure anywhere
in the decision path — resolution, decide-client exception, timeout, malformed
decision, substitution application, even this addon's own bugs — leaves the
refusal in place and nothing is forwarded upstream.

The gate also owns DNS: the request host is resolved here exactly once, the
complete answer travels to the decision endpoint, and ``server_connect`` forces
the upstream dial onto the pinned address — mitmproxy would otherwise resolve
the hostname again at dial time, the DNS-rebinding hole #4670's
connect-to-validated-address property closes.

Requires ``connection_strategy = "lazy"`` (the runner sets it): the default
eager strategy dials the upstream before the request hook runs, contacting
destinations the decision may deny.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import socket
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import assert_never

from mitmproxy import connection, http
from mitmproxy.proxy import server_hooks

from haku.egress.decide_client import DecideClient
from haku.egress.decision import DecideAllowed, DecideDenied, PlaceholderSubstitution, RequestMeta

logger = logging.getLogger(__name__)

DEFAULT_DECIDE_TIMEOUT_SECONDS = 5.0

_FAIL_CLOSED_MESSAGE = "egress decision unavailable; refusing (fail closed)"

type ResolveAddresses = Callable[[str, int], Awaitable[frozenset[IPv4Address | IPv6Address]]]


async def resolve_addresses(host: str, port: int) -> frozenset[IPv4Address | IPv6Address]:
    """Complete system resolution of ``host``: every address the answer contained (#4670)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return frozenset(ip_address(sockaddr[0]) for _family, _type, _proto, _canonname, sockaddr in infos)


def _refusal(status: int, message: str) -> http.Response:
    return http.Response.make(status, f"{message}\n".encode(), {"content-type": "text/plain; charset=utf-8"})


def _proxy_client_bearer(value: str) -> str | None:
    """Extract the bridge bearer from Bearer or URL-userinfo Basic proxy auth.

    Runner-launched clients get credentials through ``http://:<token>@proxy`` because that is
    understood by ordinary HTTP clients. A caller may also send an explicit Bearer header. Basic
    userinfo is deliberately accepted only with an empty username: the proxy credential is one
    bearer, not a username/password pair.
    """
    scheme, header_separator, payload = value.partition(" ")
    if not header_separator or not payload.strip():
        return None
    payload = payload.strip()
    if scheme.lower() == "bearer":
        return payload
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    username, credential_separator, password = decoded.partition(b":")
    if credential_separator != b":" or username or not password:
        return None
    try:
        return password.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _take_proxy_client_bearer(flow: http.HTTPFlow, credentials: dict[str, str]) -> tuple[str | None, bool]:
    """Consume proxy auth and retain it across CONNECT's later intercepted inner requests."""
    header = flow.request.headers.get("proxy-authorization")
    client_id = flow.client_conn.id if flow.client_conn is not None else None
    if header is not None:
        # Proxy-Authorization is for this listener, never for the upstream server. Remove it even
        # when malformed so a failed attempt cannot accidentally cross the fence.
        del flow.request.headers["proxy-authorization"]
        credential = _proxy_client_bearer(header)
        if client_id is not None:
            if credential is None:
                credentials.pop(client_id, None)
            else:
                credentials[client_id] = credential
        return credential, True
    if client_id is not None and (credential := credentials.get(client_id)) is not None:
        return credential, True
    return None, False


def _swap_placeholder(header_value: str, substitution: PlaceholderSubstitution) -> str:
    """Iron-proxy replace semantics: substring swap, reaching inside base64 ``Basic`` payloads."""
    swapped = header_value.replace(substitution.placeholder, substitution.value)
    if swapped != header_value:
        return swapped
    scheme, sep, payload = header_value.partition(" ")
    if sep and scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(payload, validate=True)
        except binascii.Error:
            return header_value  # not base64, so nothing recognizable to swap
        swapped_payload = decoded.replace(substitution.placeholder.encode(), substitution.value.encode())
        if swapped_payload != decoded:
            return f"{scheme} {base64.b64encode(swapped_payload).decode()}"
    return header_value


def _apply_substitution(request: http.Request, substitution: PlaceholderSubstitution) -> bool:
    """Swap the placeholder wherever a scanned header presents it; True if anything changed."""
    applied = False
    for name in substitution.match_headers:
        values = request.headers.get_all(name)
        swapped = [_swap_placeholder(value, substitution) for value in values]
        if swapped != values:
            request.headers.set_all(name, swapped)
            applied = True
    return applied


class EgressGateAddon:
    def __init__(
        self,
        decide: DecideClient,
        decide_timeout_seconds: float = DEFAULT_DECIDE_TIMEOUT_SECONDS,
        resolve: ResolveAddresses = resolve_addresses,
    ) -> None:
        self._decide = decide
        self._decide_timeout_seconds = decide_timeout_seconds
        self._resolve = resolve
        # The validated address each allowed destination must dial. Keyed by the exact
        # (host, port) the decision was made for, because ``server_connect`` sees only the
        # connection, not the flow: a dial therefore uses the latest allow's validated address
        # for that destination — always one a decide exchange validated for exactly this
        # (host, port), possibly from a newer allow than the flow that triggered the dial.
        # Grows by distinct destinations seen; never shrinks.
        self._pinned_upstreams: dict[tuple[str, int], IPv4Address | IPv6Address] = {}
        # CONNECT's inner intercepted request is a separate Flow from the CONNECT flow, but both
        # belong to the same client connection. Keep the bridge bearer there until disconnect so
        # the inner request cannot be mistaken for an unauthenticated new client.
        self._proxy_client_credentials: dict[str, str] = {}

    def client_disconnected(self, client: connection.Client) -> None:
        self._proxy_client_credentials.pop(client.id, None)

    async def http_connect(self, flow: http.HTTPFlow) -> None:
        # A non-2xx response on a CONNECT flow makes mitmproxy refuse the tunnel.
        client_credential, supplied = _take_proxy_client_bearer(flow, self._proxy_client_credentials)
        meta = RequestMeta(
            method=flow.request.method, scheme=None, host=flow.request.host, port=flow.request.port, path=None
        )
        await self._gate(flow, meta, client_credential=client_credential, credential_supplied=supplied)

    async def request(self, flow: http.HTTPFlow) -> None:
        request = flow.request
        client_credential, supplied = _take_proxy_client_bearer(flow, self._proxy_client_credentials)
        meta = RequestMeta(
            method=request.method, scheme=request.scheme, host=request.host, port=request.port, path=request.path
        )
        await self._gate(flow, meta, client_credential=client_credential, credential_supplied=supplied)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Stream the upstream response body to the client instead of buffering it whole.

        By this hook the flow was already admitted: the ``request``/``http_connect`` gate ran and
        allowed it before the upstream was dialed, so streaming the body back bypasses no decision
        — bodies never enter decide calls and ``match_headers`` scanning never touches them (#4670).
        Without this, mitmproxy buffers the entire body before forwarding, so a large download (a
        ``bbr`` → BuildBuddy gRPC stream, a multi-hundred-MB artifact) would not reach the client
        until the upstream finished serving it, and the proxy's memory would track the body size
        (#4914).

        Response-direction only, set per-flow rather than via the global ``stream_large_bodies``
        option. Request-body streaming is deliberately not enabled here: mitmproxy dials the
        upstream and streams the request body from ``start_request_stream`` *before* the ``request``
        hook fires, so a streamed request to an already-pinned destination would reach the upstream
        ahead of its own per-request decision — fail open (#4670). Enabling it safely requires first
        moving the gate to ``requestheaders`` (before the dial); that is a separate change (see
        haku/egress/TODO.md), so large request bodies stay buffered and gated as today.

        Synthetic gate refusals already carry materialized content and are sent directly, so setting
        ``stream`` on them is a no-op — only real upstream responses take the streaming path.

        Informational (1xx) responses are left alone: a ``101 Switching Protocols`` upgrade whose
        headers do not end the stream would otherwise be misrouted into response-body streaming
        instead of the WebSocket/upgrade switch, and ``100 Continue`` carries no body to stream.
        """
        if flow.response is not None and flow.response.status_code >= 200:
            flow.response.stream = True

    def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        """Force the upstream dial onto the address its decide exchange validated.

        mitmproxy resolves ``Server.address`` itself at connect time, so a short-TTL
        rebinding answer could swap the validated public address for a private one between
        decision and connection (#4670). The override rewrites only ``Server.address``:
        ``Server.sni`` and the Host header keep the real hostname, so upstream TLS still
        verifies the real hostname against the real upstream certificate. A dial for a
        destination no allow pinned is refused — setting ``Server.error`` makes mitmproxy
        kill the connection before connecting (fail closed).
        """
        assert data.server.address is not None  # mitmproxy refuses address-less dials before this hook
        host, port = data.server.address[0], data.server.address[1]
        pinned = self._pinned_upstreams.get((host, port))
        if pinned is None:
            data.server.error = "egress dial without a validated pinned address; refusing (fail closed)"
            return
        data.server.address = (str(pinned), port)

    async def _gate(
        self, flow: http.HTTPFlow, meta: RequestMeta, *, client_credential: str | None, credential_supplied: bool
    ) -> None:
        flow.response = _refusal(502, _FAIL_CLOSED_MESSAGE)
        if client_credential is None:
            reason = "invalid proxy client bearer" if credential_supplied else "proxy client bearer required"
            flow.response = _refusal(407, reason)
            return
        try:
            async with asyncio.timeout(self._decide_timeout_seconds):
                # One resolution per admission: the decision validates this complete answer,
                # and the deterministic minimum (wire serialization order: IPv4 before IPv6,
                # then numeric) is the address server_connect will dial verbatim.
                resolved = await self._resolve(meta.host, meta.port)
                upstream_ip = min(resolved, key=lambda address: (address.version, int(address)))
                decision = await self._decide.decide(
                    meta, resolved_ips=resolved, upstream_ip=upstream_ip, proxy_client_credential=client_credential
                )
            match decision:
                case DecideDenied():
                    logger.info("deny %s %s:%d: %s", meta.method, meta.host, meta.port, decision.reason)
                    flow.response = _refusal(403, f"egress denied: {decision.reason}")
                case DecideAllowed():
                    self._pinned_upstreams[(meta.host, meta.port)] = upstream_ip
                    applied = sum(
                        _apply_substitution(flow.request, substitution) for substitution in decision.substitutions
                    )
                    logger.info(
                        "allow %s %s:%d -> %s decision_id=%s (substitutions: %d of %d applied)",
                        meta.method,
                        meta.host,
                        meta.port,
                        upstream_ip,
                        decision.decision_id,
                        applied,
                        len(decision.substitutions),
                    )
                    flow.response = None  # cleared last: everything that can fail has already run
                case _:
                    assert_never(decision)
        except Exception as e:
            # Exception type only: messages and tracebacks can embed substitution
            # values (pydantic validation input dumps, invalid header values), and
            # #4670 requires that credential values never enter proxy logs.
            # warning, not error: the fence held.
            logger.warning(
                "egress decision failed (%s) for %s %s:%d; refusing",
                type(e).__name__,
                meta.method,
                meta.host,
                meta.port,
            )
            flow.response = _refusal(502, _FAIL_CLOSED_MESSAGE)

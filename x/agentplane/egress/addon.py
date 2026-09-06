"""The mitmproxy addon: every CONNECT and every request is refused unless identity and policy allow it.

Each hook pre-sets a refusal and replaces it only at the very end of a successful path, so a failure
anywhere — a missing token, an API server that does not answer, a bug in this addon — leaves the
refusal in place and nothing reaches the upstream (stock mitmproxy would log an addon exception and
let the flow continue). The identity travels in `Proxy-Authorization: Bearer <token>`; a tunnel's
inner requests cannot carry it, so the token is remembered per client connection until it closes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import ip_address

from mitmproxy import connection, http
from mitmproxy.proxy import server_hooks

from x.agentplane.egress.decisions import DecisionRecord, DecisionRing, Outcome
from x.agentplane.egress.identity import IdentityRejectedError, PodIdentity, PodIdentityVerifier
from x.agentplane.egress.policy import (
    CONNECT,
    Allowed,
    AuthenticatedWorkloadContext,
    Decision,
    Denied,
    DenyReason,
    EgressRequest,
    Index,
    evaluate,
)
from x.agentplane.egress.resources import Sandbox
from x.agentplane.egress.rules_api import RulesApi, SandboxNotCurrentError
from x.agentplane.egress.upstream import Pin, UpstreamRefusedError, UpstreamResolver

logger = logging.getLogger(__name__)

DENIED_HEADER = "x-agentplane-egress"


@dataclass(frozen=True)
class _AuthenticatedConnection:
    """A bearer associated with a client connection only after successful verification."""

    token: str = field(repr=False)
    identity: PodIdentity


def _refusal(reason: DenyReason) -> http.Response:
    status = 502 if reason in {DenyReason.UNAVAILABLE, DenyReason.HOST_UNRESOLVED} else 403
    return http.Response.make(status, b"", {DENIED_HEADER: f"denied; reason={reason}"})


def _peer_ip(flow: http.HTTPFlow) -> str:
    peername = flow.client_conn.peername
    if peername is None:
        raise IdentityRejectedError(DenyReason.POD_MISMATCH, "client connection has no peer address")
    address = ip_address(peername[0])
    return str(address.ipv4_mapped or address) if address.version == 6 else str(address)


class EgressAddon:
    def __init__(
        self,
        *,
        index: Index,
        verifier: PodIdentityVerifier,
        ring: DecisionRing,
        resolver: UpstreamResolver,
        rules_api: RulesApi,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._index = index
        self._verifier = verifier
        self._ring = ring
        self._resolver = resolver
        self._rules_api = rules_api
        self._clock = clock
        self._authenticated: dict[str, _AuthenticatedConnection] = {}

    def client_disconnected(self, client: connection.Client) -> None:
        self._authenticated.pop(client.id, None)

    async def _serve_rules(self, flow: http.HTTPFlow) -> None:
        """Serve the rules API after the same hop authentication every request requires."""
        try:
            sandbox = await self._sandbox_of(flow)
        except IdentityRejectedError as error:
            logger.info("identity rejected for the agent view: %s", error.reason)
            flow.response = _refusal(error.reason)
            return
        try:
            response = self._rules_api.request(
                flow.request.path, sandbox_name=sandbox.metadata.name, sandbox_uid=sandbox.metadata.uid
            )
        except SandboxNotCurrentError:
            # The index changed between hop authentication and projection. Never answer for a
            # replaced Sandbox or fall back to caller-supplied identity metadata.
            flow.response = _refusal(DenyReason.SANDBOX_UNKNOWN)
            return
        flow.response = http.Response.make(response.status, response.body, {"content-type": response.content_type})

    async def http_connect(self, flow: http.HTTPFlow) -> None:
        # A non-2xx response on the CONNECT flow makes mitmproxy refuse the tunnel.
        await self._gate(flow)

    async def request(self, flow: http.HTTPFlow) -> None:
        await self._gate(flow)

    def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        """Every dial goes to the address the gate checked, or nowhere."""
        self._resolver.redirect(data.server)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Stream admitted responses instead of buffering them whole; a refusal has no body to stream."""
        if flow.response is not None and flow.response.status_code >= 200:
            flow.response.stream = True

    def _token_for_authentication(self, flow: http.HTTPFlow) -> str | None:
        """A presented bearer, or one retained only after this connection authenticated before."""
        client_id = flow.client_conn.id
        header: str | None = flow.request.headers.get("proxy-authorization")
        if header is None:
            authenticated = self._authenticated.get(client_id)
            return authenticated.token if authenticated is not None else None
        # For this proxy only, never for the upstream: removed even when malformed.
        del flow.request.headers["proxy-authorization"]
        # A new hop credential must stand on its own. It can never fall back to an earlier tunnel's
        # authenticated state when malformed or rejected.
        self._authenticated.pop(client_id, None)
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        return token

    async def _admit_rules_tunnel(self, flow: http.HTTPFlow) -> None:
        """Open the tunnel to the rules API name, having proved the sandbox at the CONNECT.

        Identity is checked here rather than only on the inner request so a caller that cannot prove
        one is refused before a TLS handshake it would learn nothing from.
        """
        try:
            await self._sandbox_of(flow)
        except IdentityRejectedError as error:
            logger.info("identity rejected for the agent view's tunnel: %s", error.reason)
            flow.response = _refusal(error.reason)
            return
        # No response is what admits a CONNECT; the inner request is answered by `_serve_rules`.
        flow.response = None

    async def _sandbox_of(self, flow: http.HTTPFlow) -> Sandbox:
        """The live Sandbox this connection's token proves, or IdentityRejectedError saying why not."""
        sandbox, _ = await self._authenticate(flow)
        return sandbox

    async def _authenticate(self, flow: http.HTTPFlow) -> tuple[Sandbox, AuthenticatedWorkloadContext]:
        """Authenticate this hop or tunnel context and bind its bearer to the resulting Sandbox."""
        client_id = flow.client_conn.id
        previous = (
            self._authenticated.get(client_id) if flow.request.headers.get("proxy-authorization") is None else None
        )
        token = self._token_for_authentication(flow)
        if token is None:
            raise IdentityRejectedError(DenyReason.TOKEN_MISSING, "no bearer token in Proxy-Authorization")
        try:
            identity = await self._verifier.identify(token, _peer_ip(flow))
        except IdentityRejectedError:
            self._authenticated.pop(client_id, None)
            raise
        if previous is not None and previous.identity != identity:
            self._authenticated.pop(client_id, None)
            raise IdentityRejectedError(DenyReason.POD_MISMATCH, "authenticated tunnel identity changed")
        sandbox = self._index.sandboxes.get(identity.sandbox_name)
        if sandbox is None or sandbox.metadata.uid != identity.sandbox_uid:
            self._authenticated.pop(client_id, None)
            raise IdentityRejectedError(
                DenyReason.SANDBOX_UNKNOWN, f"Sandbox {identity.sandbox_name} is not in the index"
            )
        self._authenticated[client_id] = _AuthenticatedConnection(token=token, identity=identity)
        return sandbox, AuthenticatedWorkloadContext(
            bearer=token, sandbox_name=identity.sandbox_name, sandbox_uid=identity.sandbox_uid, pod_uid=identity.pod_uid
        )

    async def _gate(self, flow: http.HTTPFlow) -> None:
        flow.response = _refusal(DenyReason.UNAVAILABLE)
        request = flow.request
        if self._rules_api.serves(request.host):
            # The stable bootstrap name is not DNS-routable. Nothing leaves the proxy process, so
            # there is no egress decision or upstream dial to record; RulesApi owns the API and
            # projection contract rather than this transport hook.
            if request.method == CONNECT:
                await self._admit_rules_tunnel(flow)
            else:
                await self._serve_rules(flow)
            return
        egress = EgressRequest(
            method=request.method,
            host=request.host,
            port=request.port,
            path=None if request.method == CONNECT else request.path,
            headers={name.lower(): request.headers.get_all(name) for name in set(request.headers.keys())},
        )
        sandbox_name: str | None = None
        pin: Pin | None = None
        decision: Decision
        try:
            sandbox, authenticated_workload = await self._authenticate(flow)
            sandbox_name = sandbox.metadata.name
            decision = evaluate(
                self._index, sandbox, egress, self._clock(), authenticated_workload=authenticated_workload
            )
            if isinstance(decision, Allowed):
                pin = await self._resolver.pin(egress.host, egress.port, internal=decision.cluster_internal)
        except IdentityRejectedError as error:
            logger.info("identity rejected for %s %s:%d: %s", egress.method, egress.host, egress.port, error.reason)
            decision = Denied(error.reason)
        except UpstreamRefusedError as error:
            logger.info("upstream refused for %s %s:%d: %s", egress.method, egress.host, egress.port, error)
            decision = Denied(error.reason)
        except Exception as error:
            # Type only: a message could carry a header value.
            logger.warning(
                "decision failed (%s) for %s %s:%d; refusing",
                type(error).__name__,
                egress.method,
                egress.host,
                egress.port,
            )
            decision = Denied(DenyReason.UNAVAILABLE)
        common = {
            "at": self._clock(),
            "sandbox": sandbox_name,
            "method": egress.method,
            "host": egress.host,
            "port": egress.port,
            "path": egress.path,
        }
        match decision:
            case Allowed():
                self._ring.record(
                    DecisionRecord(
                        **common,
                        outcome=Outcome.ALLOW,
                        binding=decision.binding,
                        policy=decision.policy,
                        rule=decision.rule,
                        substituted=bool(decision.rewrites),
                        address=str(pin.address) if pin is not None else None,
                    )
                )
                for rewrite in decision.rewrites:
                    request.headers.set_all(rewrite.header, list(rewrite.values))
                flow.response = None  # cleared last: everything that can fail has already run
            case Denied():
                self._ring.record(DecisionRecord(**common, outcome=Outcome.DENY, reason=decision.reason))
                flow.response = _refusal(decision.reason)

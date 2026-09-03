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
from datetime import UTC, datetime
from ipaddress import ip_address

from mitmproxy import connection, http
from mitmproxy.proxy import server_hooks

from x.agentplane.egress.decisions import DecisionRecord, DecisionRing, Outcome
from x.agentplane.egress.identity import IdentityRejectedError, PodIdentityVerifier
from x.agentplane.egress.policy import CONNECT, Allowed, Decision, Denied, DenyReason, EgressRequest, Index, evaluate
from x.agentplane.egress.resources import GRANTED_BY_LABEL
from x.agentplane.egress.upstream import Pin, UpstreamRefusedError, UpstreamResolver

logger = logging.getLogger(__name__)

DENIED_HEADER = "x-agentplane-egress"


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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._index = index
        self._verifier = verifier
        self._ring = ring
        self._resolver = resolver
        self._clock = clock
        self._tokens: dict[str, str] = {}

    def client_disconnected(self, client: connection.Client) -> None:
        self._tokens.pop(client.id, None)

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

    def _take_token(self, flow: http.HTTPFlow) -> str | None:
        """The bearer token of this request, or the one its connection's CONNECT carried."""
        client_id = flow.client_conn.id
        header: str | None = flow.request.headers.get("proxy-authorization")
        if header is None:
            return self._tokens.get(client_id)
        # For this proxy only, never for the upstream: removed even when malformed.
        del flow.request.headers["proxy-authorization"]
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            self._tokens.pop(client_id, None)
            return None
        self._tokens[client_id] = token
        return token

    async def _gate(self, flow: http.HTTPFlow) -> None:
        flow.response = _refusal(DenyReason.UNAVAILABLE)
        request = flow.request
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
            token = self._take_token(flow)
            if token is None:
                raise IdentityRejectedError(DenyReason.TOKEN_MISSING, "no bearer token in Proxy-Authorization")
            identity = await self._verifier.identify(token, _peer_ip(flow))
            sandbox = self._index.sandboxes.get(identity.sandbox_name)
            if sandbox is None or sandbox.metadata.uid != identity.sandbox_uid:
                raise IdentityRejectedError(
                    DenyReason.SANDBOX_UNKNOWN, f"Sandbox {identity.sandbox_name} is not in the index"
                )
            sandbox_name = sandbox.metadata.name
            decision = evaluate(self._index, sandbox, egress, self._clock())
            if isinstance(decision, Allowed):
                pin = await self._resolver.pin(egress.host, egress.port)
        except IdentityRejectedError as error:
            logger.info("identity rejected for %s %s:%d: %s", egress.method, egress.host, egress.port, error)
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
                binding = self._index.bindings.get(decision.binding)
                self._ring.record(
                    DecisionRecord(
                        **common,
                        outcome=Outcome.ALLOW,
                        binding=decision.binding,
                        granted_by=binding.metadata.labels.get(GRANTED_BY_LABEL) if binding is not None else None,
                        policy=decision.policy,
                        rule=decision.rule,
                        substituted=decision.substitution is not None,
                        address=str(pin.address) if pin is not None else None,
                    )
                )
                if decision.substitution is not None:
                    request.headers.set_all(decision.substitution.header, list(decision.substitution.values))
                flow.response = None  # cleared last: everything that can fail has already run
            case Denied():
                self._ring.record(DecisionRecord(**common, outcome=Outcome.DENY, reason=decision.reason))
                flow.response = _refusal(decision.reason)

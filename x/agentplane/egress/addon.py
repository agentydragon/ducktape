"""The mitmproxy addon: every CONNECT and every request is refused unless identity and policy allow it.

Each hook pre-sets a refusal and replaces it only at the very end of a successful path, so a failure
anywhere — a missing token, an API server that does not answer, a bug in this addon — leaves the
refusal in place and nothing reaches the upstream (stock mitmproxy would log an addon exception and
let the flow continue). The identity travels in `Proxy-Authorization: Bearer <token>`; a tunnel's
inner requests cannot carry it, so the token is remembered per client connection until it closes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from mitmproxy import connection, http
from mitmproxy.proxy import server_hooks

from x.agentplane.egress.decision_log import DecisionLog
from x.agentplane.egress.decisions import DecisionRecord, Outcome, Phase
from x.agentplane.egress.identity import IdentityRejectedError, WorkloadIdentityVerifier
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
from x.agentplane.egress.upstream import Pin, UpstreamRefusedError, UpstreamResolver
from x.agentplane.sandbox_auth.bearer import parse_bearer
from x.agentplane.sandbox_auth.principal import WorkloadPrincipal
from x.agentplane.subjects import ServiceAccountRef

logger = logging.getLogger(__name__)

DENIED_HEADER = "x-agentplane-egress"


@dataclass(frozen=True)
class _AuthenticatedConnection:
    """A bearer associated with a client connection only after successful verification."""

    token: str = field(repr=False)
    identity: WorkloadPrincipal


def _refusal(reason: DenyReason) -> http.Response:
    status = 502 if reason in {DenyReason.UNAVAILABLE, DenyReason.HOST_UNRESOLVED} else 403
    return http.Response.make(status, b"", {DENIED_HEADER: f"denied; reason={reason}"})


class EgressAddon:
    def __init__(
        self,
        *,
        index: Index,
        verifier: WorkloadIdentityVerifier,
        decision_log: DecisionLog,
        resolver: UpstreamResolver,
        stale_after_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._producer_id = uuid4()
        self._index = index
        self._stale_after_seconds = stale_after_seconds
        self._verifier = verifier
        self._decision_log = decision_log
        self._resolver = resolver
        self._clock = clock
        self._inflight: dict[str, str] = {}
        self._idle = asyncio.Event()
        self._idle.set()
        self._authenticated: dict[str, _AuthenticatedConnection] = {}

    def begin_drain(self) -> None:
        self._index.draining = True

    async def notify_draining(self) -> None:
        await self._index.notify()

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def client_connected(self, client: connection.Client) -> None:
        if self._index.draining:
            client.error = "proxy draining"

    def _finished(self, flow: http.HTTPFlow) -> None:
        self._inflight.pop(flow.id, None)
        if not self._inflight:
            self._idle.set()

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.websocket is None:
            self._finished(flow)

    def error(self, flow: http.HTTPFlow) -> None:
        self._finished(flow)

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        self._finished(flow)

    def client_disconnected(self, client: connection.Client) -> None:
        self._authenticated.pop(client.id, None)
        self._inflight = {key: owner for key, owner in self._inflight.items() if owner != client.id}
        if not self._inflight:
            self._idle.set()

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
        return parse_bearer(header)

    async def _caller_of(self, flow: http.HTTPFlow) -> ServiceAccountRef:
        """The subject this connection's token proves, or IdentityRejectedError saying why not."""
        caller, _ = await self._authenticate(flow)
        return caller

    async def _authenticate(self, flow: http.HTTPFlow) -> tuple[ServiceAccountRef, AuthenticatedWorkloadContext]:
        """Authenticate this hop or tunnel context and bind its bearer to the resulting caller."""
        client_id = flow.client_conn.id
        previous = (
            self._authenticated.get(client_id) if flow.request.headers.get("proxy-authorization") is None else None
        )
        token = self._token_for_authentication(flow)
        if token is None:
            raise IdentityRejectedError(DenyReason.TOKEN_MISSING, "no bearer token in Proxy-Authorization")
        try:
            identity = await self._verifier.identify(token)
        except IdentityRejectedError:
            self._authenticated.pop(client_id, None)
            raise
        if previous is not None and previous.identity != identity:
            self._authenticated.pop(client_id, None)
            raise IdentityRejectedError(DenyReason.POD_MISMATCH, "authenticated tunnel identity changed")
        self._authenticated[client_id] = _AuthenticatedConnection(token=token, identity=identity)
        return identity.account, AuthenticatedWorkloadContext(
            bearer=token, caller=identity.account, pod_uid=identity.pod_uid
        )

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
        subject: ServiceAccountRef | None = None
        authenticated_workload: AuthenticatedWorkloadContext | None = None
        pin: Pin | None = None
        decision: Decision
        try:
            caller, authenticated_workload = await self._authenticate(flow)
            subject = caller
            if not self._index.available(self._clock(), stale_after_seconds=self._stale_after_seconds):
                raise IdentityRejectedError(DenyReason.UNAVAILABLE, "enforcement index unavailable")
            decision = evaluate(
                self._index, caller, egress, self._clock(), authenticated_workload=authenticated_workload
            )
            if isinstance(decision, Allowed):
                admitted = decision
                pin = await self._resolver.pin(egress.host, egress.port, internal=decision.cluster_internal)
                # Authentication and DNS await I/O. Re-read policy and readiness after the last await,
                # so revocation, expiry or staleness during admission cannot authorize a later dial.
                if not self._index.available(self._clock(), stale_after_seconds=self._stale_after_seconds):
                    decision = Denied(DenyReason.UNAVAILABLE)
                else:
                    decision = evaluate(
                        self._index, caller, egress, self._clock(), authenticated_workload=authenticated_workload
                    )
                    if decision != admitted:
                        decision = Denied(DenyReason.UNAVAILABLE)
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
            "subject": subject,
            "method": egress.method[:32],
            "host": egress.host.lower()[:253],
            "port": egress.port,
            "producer_id": self._producer_id,
            "connection_id": flow.client_conn.id,
            "phase": Phase.CONNECT if egress.method == CONNECT else Phase.HTTP_REQUEST,
            "source_pod_uid": authenticated_workload.pod_uid if authenticated_workload is not None else None,
        }
        match decision:
            case Allowed():
                self._decision_log.record(
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
                if egress.method != CONNECT:
                    self._inflight[flow.id] = flow.client_conn.id
                    self._idle.clear()
                flow.response = None  # cleared last: everything that can fail has already run
            case Denied():
                self._decision_log.record(DecisionRecord(**common, outcome=Outcome.DENY, reason=decision.reason))
                flow.response = _refusal(decision.reason)

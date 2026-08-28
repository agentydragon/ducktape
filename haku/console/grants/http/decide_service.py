"""Console-side evaluator behind ``POST /api/internal/http/decide`` (#4670, #4884).

The decision endpoint is the oracle of the egress fence: it converts an authenticated caller
identity plus concrete request metadata into a reachability verdict and the request-specific
credential substitutions. Two credentials arrive with every call and neither is a general
Agent/Operator credential:

- the **proxy identity bearer** in ``Authorization`` — the console-side static bearer the
  colocated proxy holds; rejected calls never reach evaluation;
- the **Agent-bound fence credential** in the body — endpoint-scoped by construction: resolved
  only here, never registered with ``AgentBearerAuthority``, so it is invalid for MCP, session,
  and operator APIs, and those bearers are invalid here.

Every error path denies: an unknown fence credential, ungrantable metadata, or a grant-authority
failure never admits, and the proxy fails closed on any non-2xx response.

Reaching a cluster-internal destination (#4948 override). By default a resolved answer touching
prohibited address space denies outright — the always-prohibited classes and the deploy's
``prohibited_cidrs`` are the app-layer boundary that keeps a fenced Agent off the cluster's own
network. A standing policy or temporary grant may carry ``allow_prohibited_address`` to lift that
denial, but only for its own exact origin and only when the host resolves *entirely* into
prohibited space: this is the reusable, destination-scoped primitive for granting one Agent access
to one specific internal service (an in-cluster model gateway, say), never a global private-address
bypass. A mixed public+prohibited answer stays denied as a rebinding signature regardless of the
flag. A broader, principled model for Agent access to internal services is deliberately future
work; this primitive is the minimum that unblocks the concrete case without foreclosing it.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

from pydantic import SecretStr

from haku.console.grants.http.decide_config import LoadedEgressDecide
from haku.console.grants.http.models import HttpMethod, HttpOrigin, HttpRequestAllowed, HttpScheme
from haku.console.grants.http.service import HttpGrantService
from haku.console.grants.principal import RequestPrincipal
from haku.egress.decision import (
    DecideAllowed,
    DecideDenied,
    DecideRequest,
    DecisionSource,
    GrantScope,
    PlaceholderSubstitution,
    RequestMeta,
)

logger = logging.getLogger(__name__)

CONNECT_METHOD = "CONNECT"


class HttpDecideUnavailableError(RuntimeError):
    """The Console cannot make an authoritative egress decision."""


def _prohibited_address_class(address: IPv4Address | IPv6Address) -> str | None:
    """The always-prohibited class containing ``address``, or None for a public address.

    ``is_private`` is deliberately the broad net: beyond RFC1918 and v6 ULA it covers the
    whole IANA special-purpose registry (documentation/TEST-NET, benchmarking, reserved,
    and v4-mapped v6 by delegation) — nothing in it is ever a legitimate egress destination.
    """
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_multicast:
        return "multicast"
    if address.is_unspecified:
        return "unspecified"
    if address.is_private:
        return "in a private range"
    return None


@dataclass(frozen=True, slots=True)
class _Tunnel:
    """A CONNECT the proxy has not decrypted: no inner request exists yet."""

    origin: HttpOrigin


@dataclass(frozen=True, slots=True)
class _InnerRequest:
    """A decrypted (or plain) HTTP request; ``path`` is the path plus query as the proxy sends it."""

    origin: HttpOrigin
    method: HttpMethod
    path: str


def _canonicalize(meta: RequestMeta) -> _Tunnel | _InnerRequest | DecideDenied:
    """Project wire metadata onto the grant vocabulary, or deny what that vocabulary cannot admit.

    Canonicalization failures are policy denials, not server errors: an IP-literal host, an
    ungrantable method, or incoherent CONNECT metadata can never be covered by any grant, so the
    caller gets a reasoned deny with no grantable scope.
    """
    if meta.method == CONNECT_METHOD:
        if meta.scheme is not None or meta.path is not None:
            return DecideDenied(reason="malformed CONNECT metadata")
        # An opaque tunnel transports TLS, so only https-origin grants can admit it; interception
        # yields inner requests that are each decided individually.
        try:
            return _Tunnel(origin=HttpOrigin(scheme=HttpScheme.HTTPS, host=meta.host, port=meta.port))
        except ValueError:
            return DecideDenied(reason="origin is not grantable")
    if meta.scheme is None or meta.path is None or not meta.path.startswith("/"):
        return DecideDenied(reason="malformed request metadata")
    try:
        method = HttpMethod(meta.method)
    except ValueError:
        return DecideDenied(reason="method is not grantable")
    try:
        origin = HttpOrigin(scheme=HttpScheme(meta.scheme), host=meta.host, port=meta.port)
    except ValueError:
        return DecideDenied(reason="origin is not grantable")
    return _InnerRequest(origin=origin, method=method, path=meta.path)


class HttpDecideService:
    """Authenticate the proxy, bind the fence credential to its Agent, evaluate, fail closed.

    The resolved answer is validated alongside the authorities: an answer touching prohibited
    address space — the always-on classes of ``_prohibited_address_class`` or a deploy-configured
    prohibited CIDR — denies (#4948) unless a matching standing entry or grant carries
    ``allow_prohibited_address`` and the host resolves *entirely* into prohibited space, the
    destination-scoped internal-service override (module docstring). A mixed public+prohibited
    answer is the rebinding signature and denies regardless of the flag.
    Evaluation order is #4670's: standing HTTP policy first, then the principal's
    active temporary grants after a clean standing denial. Standing policy is the deploy-managed
    ``egress_decide.standing_policies`` config (#4941): reviewed durable allowances whose
    admissions carry ``standing:<entry id>`` provenance and no deadline, since only a redeploy —
    which restarts Console and proxy together — changes them.
    """

    def __init__(
        self,
        *,
        grants: HttpGrantService,
        credentials: LoadedEgressDecide,
        prohibited_cidrs: frozenset[IPv4Network | IPv6Network],
    ) -> None:
        self._grants = grants
        self._credentials = credentials
        self._egress_credentials = {credential.handle: credential for credential in credentials.credentials}
        self._standing_policies = credentials.standing_policies
        self._prohibited_cidrs = sorted(prohibited_cidrs, key=str)

    def authenticate_proxy(self, authorization: str) -> bool:
        """Whether ``Authorization`` presents exactly the configured proxy identity bearer."""
        token = _bearer_token(authorization)
        return token is not None and secrets.compare_digest(token, self._credentials.proxy_token.get_secret_value())

    def _resolve_fence_credential(self, fence_credential: SecretStr) -> RequestPrincipal | None:
        presented = fence_credential.get_secret_value()
        for credential in self._credentials.fence_credentials:
            if secrets.compare_digest(presented, credential.token.get_secret_value()):
                # Configured fence credentials are static: no live-session identity, so
                # exact-session grants are not exercisable through them (grants/principal.py).
                return RequestPrincipal(agent_id=credential.agent_id, session_id=None, access_profile_id=None)
        return None

    def _prohibited_label(self, address: IPv4Address | IPv6Address) -> str | None:
        """The always-on class or deploy-CIDR label prohibiting ``address``, or None if it is public."""
        if class_label := _prohibited_address_class(address):
            return class_label
        for network in self._prohibited_cidrs:
            if address in network:
                return f"in prohibited range {network}"
        return None

    def _prohibited_answer_reason(self, request: DecideRequest) -> str | None:
        """Denial reason when the resolved answer touches prohibited address space; None when clean.

        The complete answer is validated, not only the pinned address, and one prohibited member is
        enough to produce a reason. What that reason then means splits on whether the answer is
        *fully* prohibited (an internal destination a flagged allowance may override —
        :meth:`_all_addresses_prohibited`) or *mixed* public+prohibited (the DNS-rebinding
        signature, refused outright rather than filtered to its public members, #4948).
        """
        for address in sorted(request.resolved_ips, key=lambda address: (address.version, int(address))):
            if label := self._prohibited_label(address):
                return f"resolved address {address} is {label}"
        return None

    def _all_addresses_prohibited(self, request: DecideRequest) -> bool:
        """Whether *every* resolved address is prohibited — the only shape a flagged allowance may
        override. A mixed answer is not, so its rebinding refusal stands whatever the flag says."""
        return all(self._prohibited_label(address) is not None for address in request.resolved_ips)

    async def decide(self, request: DecideRequest) -> DecideAllowed | DecideDenied:
        meta = request.request
        principal = self._resolve_fence_credential(request.fence_credential)
        if principal is None:
            logger.info("egress decision deny %s %s:%d: unknown fence credential", meta.method, meta.host, meta.port)
            return DecideDenied(reason="unknown fence credential")
        prohibited_reason = self._prohibited_answer_reason(request)
        # A fully-internal answer is overridable by an allowance carrying allow_prohibited_address;
        # a mixed public+prohibited answer is the rebinding signature and is never overridable.
        overridable = prohibited_reason is not None and self._all_addresses_prohibited(request)
        if prohibited_reason is not None and not overridable:
            logger.info(
                "egress decision deny agent=%s %s %s:%d: %s",
                principal.agent_id,
                meta.method,
                meta.host,
                meta.port,
                prohibited_reason,
            )
            # No grant_scope: a mixed public+prohibited answer is a rebinding signature, never a
            # grantable origin.
            return DecideDenied(reason=prohibited_reason)
        canonical = _canonicalize(meta)
        if isinstance(canonical, DecideDenied):
            logger.info(
                "egress decision deny agent=%s %s %s:%d: %s",
                principal.agent_id,
                meta.method,
                meta.host,
                meta.port,
                canonical.reason,
            )
            return canonical
        origin = canonical.origin
        standing = self._standing_decision(
            principal=principal, canonical=canonical, require_prohibited_address_allowance=overridable
        )
        if standing is not None:
            return standing
        try:
            if isinstance(canonical, _Tunnel):
                decision = await self._grants.match_tunnel(
                    request_principal=principal, origin=origin, require_prohibited_address_allowance=overridable
                )
            else:
                decision = await self._grants.match_request(
                    request_principal=principal,
                    method=canonical.method,
                    origin=origin,
                    path=canonical.path,
                    require_prohibited_address_allowance=overridable,
                )
        except Exception as error:
            # The route converts this to a plain 503, so the underlying failure surfaces only here.
            logger.exception("egress grant authority failure")
            raise HttpDecideUnavailableError("HTTP grant authority is unavailable") from error
        if isinstance(decision, HttpRequestAllowed):
            decision_id = f"grant:{decision.grant_id}"
            # A tunnel has no inner request yet, so there is nothing to substitute into; each
            # intercepted request is decided — and substituted — individually.
            substitutions = (
                []
                if isinstance(canonical, _Tunnel)
                else self._substitutions_for(principal=principal, origin=origin, handles=decision.credential_handles)
            )
            logger.info(
                "egress decision allow agent=%s %s %s://%s:%d decision_id=%s valid_until=%s credential_handles=%s",
                principal.agent_id,
                meta.method,
                origin.scheme,
                origin.host,
                origin.port,
                decision_id,
                decision.expires_at.isoformat(),
                sorted(decision.credential_handles),
            )
            return DecideAllowed(
                source=DecisionSource.GRANT,
                decision_id=decision_id,
                valid_until=decision.expires_at,
                substitutions=substitutions,
            )
        if prohibited_reason is not None:
            # Fully-internal answer that no allow_prohibited_address allowance covered: deny with the
            # address reason and no grantable scope, exactly as an unflagged prohibited answer does —
            # the overriding flag is operator-reviewed config, never something to request.
            logger.info(
                "egress decision deny agent=%s %s %s://%s:%d: %s",
                principal.agent_id,
                meta.method,
                origin.scheme,
                origin.host,
                origin.port,
                prohibited_reason,
            )
            return DecideDenied(reason=prohibited_reason)
        logger.info(
            "egress decision deny agent=%s %s %s://%s:%d: %s",
            principal.agent_id,
            meta.method,
            origin.scheme,
            origin.host,
            origin.port,
            decision.reason,
        )
        return DecideDenied(
            reason=decision.reason, grant_scope=GrantScope(scheme=origin.scheme, host=origin.host, port=origin.port)
        )

    def _standing_decision(
        self,
        *,
        principal: RequestPrincipal,
        canonical: _Tunnel | _InnerRequest,
        require_prohibited_address_allowance: bool,
    ) -> DecideAllowed | None:
        """Evaluate deploy-managed standing policy; ``None`` is the clean denial grants follow.

        Entries may overlap: the first declared match names the decision — declaration order in
        the reviewed config is the one stable, reviewable tiebreak — and every matching entry's
        credential redeems, mirroring how overlapping grants union their handles. A tunnel is
        admitted by an origin match alone (its ``https`` scheme is structural, so cleartext-origin
        entries can never admit one) with method/path pins binding each decrypted inner request;
        it carries no substitutions because no inner request exists yet.

        ``require_prohibited_address_allowance`` filters to entries carrying
        ``allow_prohibited_address``: the caller sets it for a fully-internal resolution, so an
        unflagged entry cannot admit an internal destination even at a matching origin.
        """
        origin = canonical.origin
        matching = [
            entry
            for entry in self._standing_policies
            if principal.agent_id in entry.agent_ids
            and origin in entry.origins
            and (isinstance(canonical, _Tunnel) or entry.coverage.covers(method=canonical.method, path=canonical.path))
            and (not require_prohibited_address_allowance or entry.allow_prohibited_address)
        ]
        if not matching:
            return None
        decision_id = f"standing:{matching[0].id}"
        handles = frozenset(entry.credential_handle for entry in matching if entry.credential_handle is not None)
        substitutions = (
            []
            if isinstance(canonical, _Tunnel)
            else self._substitutions_for(principal=principal, origin=origin, handles=handles)
        )
        logger.info(
            "egress decision allow agent=%s %s %s://%s:%d decision_id=%s credential_handles=%s",
            principal.agent_id,
            CONNECT_METHOD if isinstance(canonical, _Tunnel) else canonical.method,
            origin.scheme,
            origin.host,
            origin.port,
            decision_id,
            sorted(handles),
        )
        return DecideAllowed(source=DecisionSource.STANDING, decision_id=decision_id, substitutions=substitutions)

    def _substitutions_for(
        self, *, principal: RequestPrincipal, origin: HttpOrigin, handles: frozenset[str]
    ) -> list[PlaceholderSubstitution]:
        """Resolve the credential handles named by the matching allowances — temporary grants or
        standing entries — into this request's substitutions.

        Credential redemption is an authority separate from reachability (#4670): a handle that is
        not configured, not assigned to the Agent, or not redeemable at this origin yields no
        substitution while the admission stands — the inert placeholder then passes through
        verbatim and is worthless upstream (#4884 placeholder ruling). Each such refusal is an
        operator-visible mismatch between a durable allowance and the deploy config, hence the
        warnings; they name only inert handles, never values.
        """
        substitutions: list[PlaceholderSubstitution] = []
        for handle in sorted(handles):
            credential = self._egress_credentials.get(handle)
            if credential is None:
                logger.warning("egress credential %s named by a matched allowance is not configured", handle)
                continue
            if principal.agent_id not in credential.agent_ids:
                logger.warning("egress credential %s is not assigned to agent %s", handle, principal.agent_id)
                continue
            if origin not in credential.origins:
                logger.warning(
                    "egress credential %s is not redeemable at %s://%s:%d",
                    handle,
                    origin.scheme,
                    origin.host,
                    origin.port,
                )
                continue
            substitutions.append(
                PlaceholderSubstitution(
                    placeholder=credential.placeholder,
                    value=credential.value.get_secret_value(),
                    match_headers=credential.match_headers,
                )
            )
        return substitutions


def _bearer_token(value: str) -> str | None:
    scheme, separator, token = value.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        return None
    return token.strip()

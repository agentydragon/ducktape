"""Console-side evaluator behind ``POST /api/internal/http/decide`` (#4670, #4884).

The decision endpoint is the oracle of the egress fence: it converts an authenticated caller
identity plus concrete request metadata into a reachability verdict and the request-specific
credential substitutions. The shared-fence credential arrives in ``Authorization`` and is not a
general Agent/Operator credential. A Console-launched sandbox also supplies its session token in
the body:

- the **shared-fence credential** in ``Authorization`` — endpoint-scoped by construction and
  resolved only here; it authenticates the shared fence, but does not identify an Agent;
- the required **session token** in the body — resolved through ``AgentBearerAuthority``
  and accepted only for a live session, then used as the exact data-plane Agent identity. It is
  the same secret the runner protocol and Console MCP authenticate. A missing or non-session
  token is denied; there is no static Agent fallback.

Every error path denies: an unknown fence credential, ungrantable metadata, or a grant-authority
failure never admits, and the proxy fails closed on any non-2xx response.

Reaching a cluster-internal destination (#4948 override). By default a resolved answer touching
prohibited address space denies outright — the always-prohibited classes and the deploy's
``prohibited_cidrs`` are the app-layer boundary that keeps a fenced Agent off the cluster's own
network. A configuration-file allowance or database grant may carry ``allow_prohibited_address`` to lift that
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

from haku.console.grants.catalog import GrantCatalog, HttpAccessAllowed
from haku.console.grants.http.decide_config import LoadedEgressDecide
from haku.console.grants.http.models import HttpMethod, HttpOrigin, HttpScheme
from haku.console.grants.principal import RequestPrincipal, grant_principal_applies_to
from haku.console.identity.agent_bearer_authority import AgentBearerAuthority
from haku.egress.decision import (
    DecideRequest,
    GrantScope,
    HttpAuthorizationAllowed,
    HttpAuthorizationDenied,
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


def _canonicalize(meta: RequestMeta) -> _Tunnel | _InnerRequest | HttpAuthorizationDenied:
    """Project wire metadata onto the grant vocabulary, or deny what that vocabulary cannot admit.

    Canonicalization failures are policy denials, not server errors: an IP-literal host, an
    ungrantable method, or incoherent CONNECT metadata can never be covered by any grant, so the
    caller gets a reasoned deny with no grantable scope.
    """
    if meta.method == CONNECT_METHOD:
        if meta.scheme is not None or meta.path is not None:
            return HttpAuthorizationDenied(reason="malformed CONNECT metadata")
        # An opaque tunnel transports TLS, so only https-origin grants can admit it; interception
        # yields inner requests that are each decided individually.
        try:
            return _Tunnel(origin=HttpOrigin(scheme=HttpScheme.HTTPS, host=meta.host, port=meta.port))
        except ValueError:
            return HttpAuthorizationDenied(reason="origin is not grantable")
    if meta.scheme is None or meta.path is None or not meta.path.startswith("/"):
        return HttpAuthorizationDenied(reason="malformed request metadata")
    try:
        method = HttpMethod(meta.method)
    except ValueError:
        return HttpAuthorizationDenied(reason="method is not grantable")
    try:
        origin = HttpOrigin(scheme=HttpScheme(meta.scheme), host=meta.host, port=meta.port)
    except ValueError:
        return HttpAuthorizationDenied(reason="origin is not grantable")
    return _InnerRequest(origin=origin, method=method, path=meta.path)


class HttpDecideService:
    """Authenticate the fence, resolve the live-session Agent, evaluate, fail closed.

    The resolved answer is validated alongside the authorities: an answer touching prohibited
    address space — the always-on classes of ``_prohibited_address_class`` or a deploy-configured
    prohibited CIDR — denies (#4948) unless a matching configuration-file grant or database grant carries
    ``allow_prohibited_address`` and the host resolves *entirely* into prohibited space, the
    destination-scoped internal-service override (module docstring). A mixed public+prohibited
    answer is the rebinding signature and denies regardless of the flag.
    Evaluation order is configuration-file HTTP grants first, then the principal's active
    database grants after a clean configuration denial. The configured entries live under
    ``egress_decide.grants`` (#4941), and their source is the configuration file:
    they carry ``config_file:<entry id>`` provenance and no end date.
    """

    def __init__(
        self,
        *,
        catalog: GrantCatalog,
        credentials: LoadedEgressDecide,
        prohibited_cidrs: frozenset[IPv4Network | IPv6Network],
        agent_bearer_authority: AgentBearerAuthority,
    ) -> None:
        self._catalog = catalog
        self._credentials = credentials
        self._agent_bearer_authority = agent_bearer_authority
        self._egress_credentials = {credential.handle: credential for credential in credentials.credentials}
        self._prohibited_cidrs = sorted(prohibited_cidrs, key=str)

    def authenticate_proxy(self, authorization: str) -> bool:
        """Whether ``Authorization`` presents exactly the shared fence bearer."""
        token = _bearer_token(authorization)
        return token is not None and secrets.compare_digest(
            token, self._credentials.fence_credential.get_secret_value()
        )

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

    async def decide(self, request: DecideRequest) -> HttpAuthorizationAllowed | HttpAuthorizationDenied:
        meta = request.request
        try:
            resolved = await self._agent_bearer_authority.resolve(request.session_token.get_secret_value())
        except Exception as error:
            logger.exception("egress proxy client authority failure")
            raise HttpDecideUnavailableError("HTTP proxy client authority is unavailable") from error
        if resolved is None or resolved.actor.session_id is None:
            logger.info("egress decision deny %s %s:%d: unknown session token", meta.method, meta.host, meta.port)
            return HttpAuthorizationDenied(reason="unknown session token")
        principal = RequestPrincipal.from_source(resolved.actor)
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
            return HttpAuthorizationDenied(reason=prohibited_reason)
        canonical = _canonicalize(meta)
        if isinstance(canonical, HttpAuthorizationDenied):
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
        try:
            if isinstance(canonical, _Tunnel):
                decision = await self._catalog.match_http_tunnel(
                    request_principal=principal, origin=origin, require_prohibited_address_allowance=overridable
                )
            else:
                decision = await self._catalog.match_http_request(
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
        if isinstance(decision, HttpAccessAllowed):
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
                decision.decision_id,
                decision.valid_until.isoformat() if decision.valid_until is not None else None,
                sorted(decision.credential_handles),
            )
            return HttpAuthorizationAllowed(
                source=decision.source,
                decision_id=decision.decision_id,
                reason=decision.reason,
                valid_until=decision.valid_until,
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
            return HttpAuthorizationDenied(reason=prohibited_reason)
        logger.info(
            "egress decision deny agent=%s %s %s://%s:%d: %s",
            principal.agent_id,
            meta.method,
            origin.scheme,
            origin.host,
            origin.port,
            decision.reason,
        )
        return HttpAuthorizationDenied(
            reason=decision.reason, grant_scope=GrantScope(scheme=origin.scheme, host=origin.host, port=origin.port)
        )

    def _substitutions_for(
        self, *, principal: RequestPrincipal, origin: HttpOrigin, handles: frozenset[str]
    ) -> list[PlaceholderSubstitution]:
        """Resolve the credential handles named by matching database or configuration-file
        grants into this request's substitutions.

        Credential redemption is an authority separate from reachability (#4670): a handle that is
        not configured, not assigned to the request principal, or not redeemable at this origin yields no
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
            if not grant_principal_applies_to(credential.principal, principal):
                logger.warning("egress credential %s is not assigned to principal %s", handle, principal)
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

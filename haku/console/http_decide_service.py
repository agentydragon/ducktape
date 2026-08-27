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
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from pydantic import SecretStr

from haku.console.grant_principal import RequestPrincipal
from haku.console.http_decide_config import LoadedEgressDecide
from haku.console.http_grant_models import HttpMethod, HttpOrigin, HttpRequestAllowed, HttpScheme
from haku.console.http_grant_service import HttpGrantService
from haku.egress.decision import DecideAllowed, DecideDenied, DecideRequest, DecisionSource, GrantScope, RequestMeta

logger = logging.getLogger(__name__)

CONNECT_METHOD = "CONNECT"


class HttpDecideUnavailableError(RuntimeError):
    """The Console cannot make an authoritative egress decision."""


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

    Evaluation order is #4670's: standing HTTP policy first, then the principal's active temporary
    grants after a clean standing denial. Deploy-managed standing HTTP destination policy is a
    separate #4670 work item; until a deployment defines one, the standing step denies cleanly and
    only grants admit.
    """

    def __init__(self, *, grants: HttpGrantService, credentials: LoadedEgressDecide) -> None:
        self._grants = grants
        self._credentials = credentials

    def authenticate_proxy(self, authorization: str) -> bool:
        """Whether ``Authorization`` presents exactly the configured proxy identity bearer."""
        token = _bearer_token(authorization)
        return token is not None and secrets.compare_digest(token, self._credentials.proxy_token.get_secret_value())

    def _resolve_fence_credential(self, fence_credential: SecretStr) -> RequestPrincipal | None:
        presented = fence_credential.get_secret_value()
        for credential in self._credentials.fence_credentials:
            if secrets.compare_digest(presented, credential.token.get_secret_value()):
                # Configured fence credentials are static: no live-session identity, so
                # exact-session grants are not exercisable through them (grant_principal.py).
                return RequestPrincipal(agent_id=credential.agent_id, session_id=None, access_profile_id=None)
        return None

    async def decide(self, request: DecideRequest) -> DecideAllowed | DecideDenied:
        meta = request.request
        principal = self._resolve_fence_credential(request.fence_credential)
        if principal is None:
            logger.info("egress decision deny %s %s:%d: unknown fence credential", meta.method, meta.host, meta.port)
            return DecideDenied(reason="unknown fence credential")
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
        try:
            if isinstance(canonical, _Tunnel):
                decision = await self._grants.match_tunnel(request_principal=principal, origin=origin)
            else:
                decision = await self._grants.match_request(
                    request_principal=principal, method=canonical.method, origin=origin, path=canonical.path
                )
        except Exception as error:
            # The route converts this to a plain 503, so the underlying failure surfaces only here.
            logger.exception("egress grant authority failure")
            raise HttpDecideUnavailableError("HTTP grant authority is unavailable") from error
        if isinstance(decision, HttpRequestAllowed):
            decision_id = f"grant:{decision.grant_id}"
            logger.info(
                "egress decision allow agent=%s %s %s://%s:%d decision_id=%s valid_until=%s",
                principal.agent_id,
                meta.method,
                origin.scheme,
                origin.host,
                origin.port,
                decision_id,
                decision.expires_at.isoformat(),
            )
            return DecideAllowed(source=DecisionSource.GRANT, decision_id=decision_id, valid_until=decision.expires_at)
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


def _bearer_token(value: str) -> str | None:
    scheme, separator, token = value.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        return None
    return token.strip()

"""Contain Haku's Agent grant lifecycle around FastMCP's OAuth proxy.

FastMCP remains the MCP-facing authorization server: it validates clients,
redirects, resources, and PKCE; owns the IdP callback; and persists and issues
the OAuth token family.  This adapter supplies the product boundary FastMCP
does not own: one Haku enrollment interaction and one opaque Haku grant per
authorized token family.

The adapter deliberately knows no Haku database models.  The application
service implements :class:`AgentGrantAuthority` and performs every atomic
Agent/name/grant/binding transition.  A downstream OAuth ``client_id`` is
passed only as client-software and credential-binding evidence.  Runtime
authority always starts at the signed ``grant_id`` and resolves through Haku.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, SupportsFloat, cast, override
from uuid import UUID

import fastmcp
import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware as FastMCPMiddleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult
from fastmcp.utilities.auth import parse_scopes
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.wrappers.base import BaseWrapper
from mcp import types as mcp_types
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import AccessToken as McpAccessToken, AuthorizeError, RefreshToken, TokenError
from starlette.authentication import AuthenticationError
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from haku.console.tool_call_actor import AgentActor
from mcp_infra.authentik_auth.fastmcp_proxy import RetryableRefreshOIDCProxy
from mcp_infra.authentik_auth.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
    VerifiedOidcPrincipal,
)
from mcp_infra.persistence import OAuthClientStorage

if TYPE_CHECKING:
    from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

_SUPPORTED_FASTMCP_VERSION = "3.4.4"
_RETRY_AFTER_SECONDS = 60
_INVALID_GRANT = "The Agent authorization grant is invalid."

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthorizationCorrelation:
    """Public, already-validated values that locate one enrollment attempt.

    This tuple is a short-lived correlation and duplicate guard.  It is never
    proof of browser identity, Operator identity, or an Agent grant.
    """

    client_id: str
    redirect_uri: str
    code_challenge: str


@dataclass(frozen=True, slots=True)
class ClientSoftwareSnapshot:
    """Untrusted client presentation metadata captured for the consent page.

    Registration provenance is intentionally absent.  FastMCP 3.4.4 rebuilds
    DCR clients without preserving ``client_id_issued_at``; consequently its
    public authorize input cannot distinguish DCR from a preregistered client.
    An HTTPS ``client_id`` is likewise only a CIMD candidate without the
    FastMCP-specific fetched document.  Haku must record that presentation-only
    classification at a boundary which actually knows it, never infer it here.

    The redirect URI is deliberately absent. FastMCP has already validated the
    exact URI in :class:`AuthorizationCorrelation`, including CIMD wildcard
    resolution, so duplicating a client-level redirect list here would create a
    second and sometimes unavailable source of truth.
    """

    client_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """The immutable public view Haku persists after FastMCP accepts authorize."""

    correlation: AuthorizationCorrelation
    client: ClientSoftwareSnapshot
    requested_scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class GrantAuthorization:
    """A current binding-backed resolution of a Haku grant.

    ``actor`` is the terminal canonical principal resolved by Haku's authority;
    none of its IDs are embedded in the OAuth token. ``client_id`` remains
    credential-binding and audit evidence, not an Agent identity key.
    """

    grant_id: UUID
    actor: AgentActor
    client_id: str
    allowed_scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class TokenFamilyEvidence:
    """Receipt proving FastMCP persisted the initial local token family."""

    access_jti: str
    refresh_jti: str | None


@dataclass(frozen=True, slots=True)
class GrantRequestContext:
    """Grant resolution available while FastMCP verifies one bearer request."""

    authorization: GrantAuthorization
    token_scopes: frozenset[str]


class OidcPrincipalResolver(Protocol):
    """Turn one upstream token response into a cryptographically verified principal."""

    async def resolve(self, token_response: Mapping[str, Any]) -> VerifiedOidcPrincipal: ...


class AgentGrantAuthority(Protocol):
    """Persistence-independent port implemented by Haku's authorization service.

    Implementations translate database and transport outages to
    :class:`AgentGrantAuthorityUnavailableError`.  All methods which return a
    ``GrantAuthorization`` must validate the grant, binding, Agent, and owning
    Operator together in one locked transaction or consistent read.
    """

    async def reserve_authorization(self, *, request: AuthorizationRequest, upstream_authorization_url: str) -> str: ...

    async def begin_exchange(
        self,
        *,
        correlation: AuthorizationCorrelation,
        client: ClientSoftwareSnapshot,
        principal: VerifiedOidcPrincipal,
        granted_scopes: frozenset[str],
    ) -> GrantAuthorization: ...

    async def record_token_family(self, *, grant_id: UUID, evidence: TokenFamilyEvidence) -> None: ...

    async def resolve_grant(
        self, *, grant_id: UUID, client_id: str, token_scopes: frozenset[str]
    ) -> GrantAuthorization: ...

    async def activate_for_tool_call(
        self, *, grant_id: UUID, client_id: str, token_scopes: frozenset[str]
    ) -> GrantAuthorization: ...

    async def revoke_grant(self, *, grant_id: UUID) -> None: ...


class StaticAgentActorResolver(Protocol):
    """Resolve one verified non-OAuth credential to a canonical active actor."""

    async def resolve_static_actor(self, access_token: AccessToken) -> AgentActor | None: ...


class DuplicateAuthorizationError(Exception):
    """The same live or tombstoned FastMCP correlation is already reserved."""


class EnrollmentRejectedError(Exception):
    """The enrollment interaction cannot authorize this terminal exchange."""


class ExchangeAlreadyClaimedError(Exception):
    """Another exchange request already won the interaction transition."""


class GrantRejectedError(Exception):
    """The referenced Haku grant/binding is not authorized for this operation."""


class AgentGrantAuthorityUnavailableError(Exception):
    """Haku cannot currently make an authoritative grant decision."""


class AgentActorAuthorityUnavailableError(Exception):
    """Haku cannot currently resolve a verified credential's canonical actor."""


class BearerVerificationUnavailableError(AuthenticationError):
    """A recognized bearer could not be verified for an operational reason.

    Haku's auth composite must preserve this marker instead of treating it as a
    clean verifier non-match.  That distinction prevents a state-store or
    upstream outage from becoming a false 401 and connector deauthorization.
    """

    status_code = 503

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class BearerFailureObservingKeyValue(BaseWrapper):
    """Preserve request-local FastMCP state-store failures before it erases them.

    FastMCP owns the prefixed/model wrappers layered over this store.  Wrapping
    the one public ``client_storage`` input therefore covers its JTI mappings,
    upstream token sets, refresh metadata, codes, transactions, and clients
    without reaching into any private store.  Observation is inert outside
    bearer verification, so endpoint operations keep their native exceptions.
    """

    def __init__(self, key_value: AsyncKeyValue) -> None:
        self.key_value = key_value

    @override
    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        try:
            value = await super().get(key, collection=collection)
            return cast(dict[str, Any] | None, value)
        except Exception as error:
            observe_bearer_operational_failure(error)
            raise

    @override
    async def get_many(self, keys: Sequence[str], *, collection: str | None = None) -> list[dict[str, Any] | None]:
        try:
            values = await super().get_many(keys, collection=collection)
            return cast(list[dict[str, Any] | None], values)
        except Exception as error:
            observe_bearer_operational_failure(error)
            raise

    @override
    async def ttl(self, key: str, *, collection: str | None = None) -> tuple[dict[str, Any] | None, float | None]:
        try:
            value_and_ttl = await super().ttl(key, collection=collection)
            return cast(tuple[dict[str, Any] | None, float | None], value_and_ttl)
        except Exception as error:
            observe_bearer_operational_failure(error)
            raise

    @override
    async def ttl_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        try:
            values_and_ttls = await super().ttl_many(keys, collection=collection)
            return cast(list[tuple[dict[str, Any] | None, float | None]], values_and_ttls)
        except Exception as error:
            observe_bearer_operational_failure(error)
            raise

    @override
    async def put(
        self, key: str, value: Mapping[str, Any], *, collection: str | None = None, ttl: SupportsFloat | None = None
    ) -> None:
        try:
            await super().put(key, value, collection=collection, ttl=ttl)
        except Exception as error:
            observe_bearer_operational_failure(error)
            raise

    @override
    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        try:
            await super().put_many(keys, values, collection=collection, ttl=ttl)
        except Exception as error:
            observe_bearer_operational_failure(error)
            raise

    @override
    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        try:
            deleted = await super().delete(key, collection=collection)
            return cast(bool, deleted)
        except Exception as error:
            observe_bearer_operational_failure(error)
            raise

    @override
    async def delete_many(self, keys: Sequence[str], *, collection: str | None = None) -> int:
        try:
            deleted = await super().delete_many(keys, collection=collection)
            return cast(int, deleted)
        except Exception as error:
            observe_bearer_operational_failure(error)
            raise


class FailureObservingJWTVerifier(JWTVerifier):
    """Classify an upstream JWKS fetch failure without weakening JWT checks."""

    @override
    async def _fetch_jwks(self) -> dict[str, Any]:
        try:
            return await super()._fetch_jwks()
        except Exception as error:
            # JWTVerifier intentionally converts its fetch/parse failures to a
            # clean non-match.  Record the operational cause before that catch.
            observe_bearer_operational_failure(error)
            raise


class AgentActorResolutionUnavailableError(ToolError):
    """A verified tool call could not resolve its canonical actor; retry is safe."""


@dataclass(frozen=True, slots=True)
class _GrantIssuanceContext:
    authorization: GrantAuthorization


@dataclass(slots=True)
class _BearerFailureObservation:
    failure: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _ReferenceGrant:
    grant_id: UUID
    client_id: str
    scopes: frozenset[str]
    jti: str


_GRANT_ISSUANCE_CONTEXT: ContextVar[_GrantIssuanceContext | None] = ContextVar(
    "haku_agent_grant_issuance_context", default=None
)
_GRANT_REQUEST_CONTEXT: ContextVar[GrantRequestContext | None] = ContextVar(
    "haku_agent_grant_request_context", default=None
)
_BEARER_FAILURE_OBSERVATION: ContextVar[_BearerFailureObservation | None] = ContextVar(
    "haku_bearer_failure_observation", default=None
)
_AGENT_ACTOR_CONTEXT: ContextVar[AgentActor | None] = ContextVar("haku_agent_actor", default=None)


def current_grant_request_context() -> GrantRequestContext | None:
    """Return the grant being checked inside the current bearer verification."""

    return _GRANT_REQUEST_CONTEXT.get()


def get_agent_actor() -> AgentActor:
    """FastMCP dependency accessor for the current authenticated Agent."""

    actor = _AGENT_ACTOR_CONTEXT.get()
    if actor is None:
        raise ToolError("Agent actor is unavailable outside an authenticated tool call")
    return actor


def observe_bearer_operational_failure(error: BaseException) -> None:
    """Preserve one operational failure before FastMCP's blanket catch erases it.

    The transparent-refresh hook below uses this directly.  A storage adapter
    may use the same request-local seam for classified Postgres/Valkey failures;
    calls made outside bearer verification are intentionally inert.
    """

    observation = _BEARER_FAILURE_OBSERVATION.get()
    if observation is not None and observation.failure is None:
        observation.failure = error


def _bearer_authentication_error(connection: HTTPConnection, error: AuthenticationError) -> Response:
    """Return an explicit retryable response for a classified bearer outage."""

    if isinstance(error, BearerVerificationUnavailableError):
        return JSONResponse(
            {"error": "temporarily_unavailable", "error_description": error.detail},
            status_code=error.status_code,
            headers={"Cache-Control": "no-store", "Retry-After": str(_RETRY_AFTER_SECONDS)},
        )
    return AuthenticationMiddleware.default_on_error(connection, error)


class HakuFailurePreservingMultiAuth(MultiAuth):
    """Try every verifier but preserve a classified Haku OAuth outage.

    Stock FastMCP 3.4.4 ``MultiAuth`` catches every source exception and turns
    it into a clean non-match.  Haku still permits a later static verifier to
    accept a credential, but if every source falls through it rethrows the
    classified outage so the custom authentication response is HTTP 503 rather
    than a connector-deauthorizing 401.
    """

    def __init__(self, *, server: AuthProvider, verifiers: list[TokenVerifier]) -> None:
        super().__init__(server=server, verifiers=verifiers)
        # Do not depend on MultiAuth._sources: it is private and precisely the
        # blanket-catching implementation this adapter contains.
        self._haku_sources: tuple[AuthProvider, ...] = (server, *verifiers)

    async def verify_token(self, token: str) -> AccessToken | None:
        unavailable: BearerVerificationUnavailableError | None = None
        for source in self._haku_sources:
            try:
                result = await source.verify_token(token)
                if result is not None:
                    return result
            except BearerVerificationUnavailableError as error:
                unavailable = unavailable or error
            except Exception:
                # Preserve FastMCP's normal independent-source fallthrough for
                # every failure which is not Haku's explicit outage marker.
                logger.debug(
                    "Token verification failed for %s, trying next source", type(source).__name__, exc_info=True
                )
        if unavailable is not None:
            raise unavailable
        return None

    def get_middleware(self) -> list[StarletteMiddleware]:
        """Render the preserved marker outside Starlette's exception layer."""

        return [
            StarletteMiddleware(
                AuthenticationMiddleware, backend=BearerAuthBackend(self), on_error=_bearer_authentication_error
            ),
            StarletteMiddleware(AuthContextMiddleware),
        ]


class HakuAgentGrantMiddleware(FastMCPMiddleware):
    """Resolve the canonical Agent actor for Agent-facing MCP requests.

    Bearer verification has already established the signed FastMCP token and
    propagated its exact ``grant_id`` claim before this middleware runs. The
    authority owns the atomic ``issued -> active`` transition; an already
    active grant returns the same binding idempotently. Both discovery and
    dispatch therefore run with the same verified Agent and Operator authority.
    """

    def __init__(
        self, grant_authority: AgentGrantAuthority, *, static_actor_resolver: StaticAgentActorResolver | None = None
    ) -> None:
        self._grant_authority = grant_authority
        self._static_actor_resolver = static_actor_resolver

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        actor = await self._resolve_actor()
        actor_token = _AGENT_ACTOR_CONTEXT.set(actor)
        try:
            return await call_next(context)
        finally:
            _AGENT_ACTOR_CONTEXT.reset(actor_token)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mcp_types.ListToolsRequest],
        call_next: CallNext[mcp_types.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        actor = await self._resolve_actor()
        actor_token = _AGENT_ACTOR_CONTEXT.set(actor)
        try:
            return await call_next(context)
        finally:
            _AGENT_ACTOR_CONTEXT.reset(actor_token)

    async def _resolve_actor(self) -> AgentActor:
        token = get_access_token()
        if token is None:
            raise ToolError("Agent grant is missing")
        if "upstream_claims" not in token.claims:
            return await self._resolve_static_actor(token)
        return await self._activate_oauth_actor(token)

    async def _resolve_static_actor(self, token: AccessToken) -> AgentActor:
        if self._static_actor_resolver is None:
            raise ToolError("Agent grant is missing")
        try:
            actor = _validate_agent_actor(await self._static_actor_resolver.resolve_static_actor(token))
        except (AgentActorAuthorityUnavailableError, AgentGrantAuthorityUnavailableError) as error:
            raise AgentActorResolutionUnavailableError(
                "Agent authorization is temporarily unavailable; retry the tool call"
            ) from error
        except GrantRejectedError:
            raise ToolError("Agent grant is missing") from None
        return actor

    async def _activate_oauth_actor(self, token: AccessToken) -> AgentActor:
        try:
            grant_id = _grant_id_from_claims(token.claims)
            client_id = _required_nonblank_string(token.client_id, field_name="client_id")
            token_scopes = frozenset(token.scopes)
        except (KeyError, TypeError, ValueError):
            raise ToolError("Agent grant is invalid") from None

        try:
            authorization = await self._grant_authority.activate_for_tool_call(
                grant_id=grant_id, client_id=client_id, token_scopes=token_scopes
            )
            _validate_grant_authorization(authorization, grant_id=grant_id, client_id=client_id, scopes=token_scopes)
        except GrantRejectedError:
            raise ToolError("Agent grant is not active") from None
        except AgentGrantAuthorityUnavailableError as error:
            raise AgentActorResolutionUnavailableError(
                "Agent authorization is temporarily unavailable; retry the tool call"
            ) from error
        return authorization.actor


class HakuAgentOAuthProxy(RetryableRefreshOIDCProxy):
    """Compose Haku's Agent grant aggregate around FastMCP 3.4.4 OAuth.

    Only ``_code_store`` read/delete crosses FastMCP's private boundary.  The
    protected claim, scope, and transparent-refresh hooks are version-pinned
    below.  Route construction, callback processing, client registration,
    transaction storage, token persistence, and token issuance stay inherited.
    """

    def __init__(
        self,
        *,
        config_url: str,
        client_id: str,
        client_secret: str,
        base_url: str,
        resource_base_url: str,
        client_storage: OAuthClientStorage,
        expected_issuer: str,
        grant_authority: AgentGrantAuthority,
    ) -> None:
        super().__init__(
            config_url=config_url,
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            resource_base_url=resource_base_url,
            require_authorization_consent="external",
            client_storage=BearerFailureObservingKeyValue(client_storage),
        )
        self._principal_resolver: OidcPrincipalResolver = AuthentikOidcPrincipalResolver(
            expected_issuer=expected_issuer,
            discovered_issuer=str(self.oidc_config.issuer) if self.oidc_config.issuer is not None else None,
            jwks_uri=str(self.oidc_config.jwks_uri) if self.oidc_config.jwks_uri is not None else None,
            signing_algorithms=self.oidc_config.id_token_signing_alg_values_supported,
            client_id=client_id,
        )
        self._grant_authority = grant_authority

    @override
    def get_token_verifier(
        self,
        *,
        algorithm: str | None = None,
        audience: str | None = None,
        required_scopes: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> TokenVerifier:
        """Add failure observation without changing FastMCP's verifier policy."""

        _ = timeout_seconds
        return FailureObservingJWTVerifier(
            jwks_uri=str(self.oidc_config.jwks_uri),
            issuer=str(self.oidc_config.issuer),
            algorithm=algorithm,
            audience=audience,
            required_scopes=required_scopes,
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        client_id = _required_client_id(client.client_id)
        correlation = AuthorizationCorrelation(
            client_id=client_id, redirect_uri=str(params.redirect_uri), code_challenge=params.code_challenge
        )
        if not correlation.code_challenge:
            raise AuthorizeError("invalid_request", "An S256 PKCE challenge is required")
        snapshot = ClientSoftwareSnapshot(client_id=client_id, display_name=client.client_name or client_id)

        # FastMCP validates and stores the transaction before Haku reserves its
        # exact public correlation.  A losing reservation leaves only a bounded
        # FastMCP transaction which expires under FastMCP's own TTL.
        upstream_url = await super().authorize(client, params)
        try:
            return await self._grant_authority.reserve_authorization(
                request=AuthorizationRequest(
                    correlation=correlation, client=snapshot, requested_scopes=frozenset(params.scopes or [])
                ),
                upstream_authorization_url=upstream_url,
            )
        except DuplicateAuthorizationError:
            raise AuthorizeError("temporarily_unavailable", "An equivalent authorization is already pending") from None
        except AgentGrantAuthorityUnavailableError:
            raise AuthorizeError("temporarily_unavailable", "Agent enrollment is temporarily unavailable") from None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        client_id = _required_client_id_for_token(client.client_id)
        code_model = await self._code_store.get(key=authorization_code.code)
        if code_model is None or code_model.client_id != client_id or authorization_code.client_id != client_id:
            raise TokenError("invalid_grant", _INVALID_GRANT)

        correlation = AuthorizationCorrelation(
            client_id=client_id,
            redirect_uri=str(authorization_code.redirect_uri),
            code_challenge=authorization_code.code_challenge,
        )
        snapshot = ClientSoftwareSnapshot(client_id=client_id, display_name=client.client_name or client_id)
        try:
            principal = await self._principal_resolver.resolve(code_model.idp_tokens)
            granted_scopes = self._effective_granted_scopes(code_model.idp_tokens, authorization_code.scopes)
            authorization = await self._grant_authority.begin_exchange(
                correlation=correlation, client=snapshot, principal=principal, granted_scopes=granted_scopes
            )
            _validate_grant_authorization(
                authorization, grant_id=authorization.grant_id, client_id=client_id, scopes=granted_scopes
            )
        except (InvalidOidcPrincipalError, EnrollmentRejectedError, GrantRejectedError):
            await self._code_store.delete(key=authorization_code.code)
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except ExchangeAlreadyClaimedError:
            # The winner may not yet have let FastMCP consume its code.
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except OidcPrincipalVerificationUnavailableError as error:
            raise _service_unavailable("Upstream identity verification is temporarily unavailable") from error
        except AgentGrantAuthorityUnavailableError as error:
            raise _service_unavailable("Agent authorization is temporarily unavailable") from error

        issue_token = _GRANT_ISSUANCE_CONTEXT.set(_GrantIssuanceContext(authorization))
        try:
            token = await super().exchange_authorization_code(client, authorization_code)
        finally:
            _GRANT_ISSUANCE_CONTEXT.reset(issue_token)

        evidence, _ = self._validate_issued_family(
            token, grant_id=authorization.grant_id, client_id=client_id, allowed_scopes=authorization.allowed_scopes
        )
        try:
            await self._grant_authority.record_token_family(grant_id=authorization.grant_id, evidence=evidence)
        except EnrollmentRejectedError:
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except AgentGrantAuthorityUnavailableError as error:
            raise _service_unavailable("Agent token-family persistence is temporarily unavailable") from error
        return token

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        client_id = _required_client_id_for_token(client.client_id)
        try:
            reference = self._reference_grant(refresh_token.token, expected_token_use="refresh")
        except Exception:
            # JWT-library validation errors are intentionally a protocol
            # boundary: an invalid local refresh credential is invalid_grant.
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        if (
            refresh_token.client_id != client_id
            or reference.client_id != client_id
            or frozenset(refresh_token.scopes) != reference.scopes
            or not frozenset(scopes) <= reference.scopes
        ):
            raise TokenError("invalid_grant", _INVALID_GRANT)
        try:
            authorization = await self._grant_authority.resolve_grant(
                grant_id=reference.grant_id, client_id=client_id, token_scopes=frozenset(scopes)
            )
            _validate_grant_authorization(
                authorization, grant_id=reference.grant_id, client_id=client_id, scopes=frozenset(scopes)
            )
        except GrantRejectedError:
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except AgentGrantAuthorityUnavailableError as error:
            raise _service_unavailable("Agent authorization is temporarily unavailable") from error

        issue_token = _GRANT_ISSUANCE_CONTEXT.set(_GrantIssuanceContext(authorization))
        try:
            token = await super().exchange_refresh_token(client, refresh_token, scopes)
        finally:
            _GRANT_ISSUANCE_CONTEXT.reset(issue_token)

        _, issued_scopes = self._validate_issued_family(
            token, grant_id=authorization.grant_id, client_id=client_id, allowed_scopes=authorization.allowed_scopes
        )
        try:
            after = await self._grant_authority.resolve_grant(
                grant_id=reference.grant_id, client_id=client_id, token_scopes=issued_scopes
            )
            _require_same_actor(authorization, after, scopes=issued_scopes)
        except GrantRejectedError:
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except AgentGrantAuthorityUnavailableError as error:
            raise _service_unavailable("Agent authorization is temporarily unavailable") from error
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            reference = self._reference_grant(token, expected_token_use="access")
        except Exception:
            # This provider owns only its signed FastMCP reference tokens.  A
            # JWT-library validation failure is a clean non-match, allowing the
            # Haku composite to try a static credential.
            return None

        try:
            authorization = await self._grant_authority.resolve_grant(
                grant_id=reference.grant_id, client_id=reference.client_id, token_scopes=reference.scopes
            )
            _validate_grant_authorization(
                authorization, grant_id=reference.grant_id, client_id=reference.client_id, scopes=reference.scopes
            )
        except GrantRejectedError:
            return None
        except AgentGrantAuthorityUnavailableError as error:
            raise BearerVerificationUnavailableError("Agent authorization is temporarily unavailable") from error

        request_token = _GRANT_REQUEST_CONTEXT.set(
            GrantRequestContext(authorization=authorization, token_scopes=reference.scopes)
        )
        observation = _BearerFailureObservation()
        observation_token = _BEARER_FAILURE_OBSERVATION.set(observation)
        try:
            validated = await super().load_access_token(token)
        finally:
            _BEARER_FAILURE_OBSERVATION.reset(observation_token)
            _GRANT_REQUEST_CONTEXT.reset(request_token)

        if validated is None:
            if observation.failure is not None:
                raise BearerVerificationUnavailableError(
                    "OAuth bearer verification is temporarily unavailable"
                ) from observation.failure
            return None

        access_token = (
            validated if isinstance(validated, AccessToken) else AccessToken.model_validate(validated.model_dump())
        )
        try:
            returned_grant_id = _grant_id_from_claims(access_token.claims)
            returned_scopes = frozenset(access_token.scopes)
            if returned_grant_id != reference.grant_id or not returned_scopes <= reference.scopes:
                raise GrantRejectedError
            after = await self._grant_authority.resolve_grant(
                grant_id=reference.grant_id, client_id=reference.client_id, token_scopes=returned_scopes
            )
            _require_same_actor(authorization, after, scopes=returned_scopes)
        except (GrantRejectedError, KeyError, TypeError, ValueError):
            return None
        except AgentGrantAuthorityUnavailableError as error:
            raise BearerVerificationUnavailableError("Agent authorization is temporarily unavailable") from error

        # Preserve the downstream client registration as audited binding
        # metadata only after Haku has resolved and authorized the grant.
        if access_token.client_id != reference.client_id:
            access_token = access_token.model_copy(update={"client_id": reference.client_id})
        return access_token

    async def revoke_token(self, token: McpAccessToken | RefreshToken) -> None:
        expected_token_use = "refresh" if isinstance(token, RefreshToken) else "access"
        try:
            reference = self._reference_grant(token.token, expected_token_use=expected_token_use)
        except Exception:
            await super().revoke_token(token)
            return

        try:
            await self._grant_authority.revoke_grant(grant_id=reference.grant_id)
        except GrantRejectedError:
            # RFC 7009 revocation is idempotent and does not disclose whether a
            # token or grant existed.  FastMCP may still clean its public state.
            pass
        except AgentGrantAuthorityUnavailableError as error:
            raise _service_unavailable("Agent revocation is temporarily unavailable") from error
        await super().revoke_token(token)

    def _translate_scopes_from_idp(self, scopes: list[str]) -> list[str]:
        translated = super()._translate_scopes_from_idp(scopes)
        issue_context = _GRANT_ISSUANCE_CONTEXT.get()
        request_context = _GRANT_REQUEST_CONTEXT.get()
        authorization = (
            issue_context.authorization
            if issue_context is not None
            else request_context.authorization
            if request_context is not None
            else None
        )
        if authorization is not None and not frozenset(translated) <= authorization.allowed_scopes:
            raise TokenError("invalid_scope", "The upstream provider broadened the authorized scopes")
        return translated

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        _ = idp_tokens
        context = _GRANT_ISSUANCE_CONTEXT.get()
        if context is None:
            raise TokenError("invalid_grant", _INVALID_GRANT)
        return {"grant_id": str(context.authorization.grant_id)}

    async def _try_transparent_refresh(self, upstream_token_set: Any) -> Any:
        """Observe retryable failures before FastMCP converts them to ``None``."""

        try:
            return await super()._try_transparent_refresh(upstream_token_set)
        except Exception as error:
            if _transient_upstream_error(error):
                observe_bearer_operational_failure(error)
            raise

    def _effective_granted_scopes(self, idp_tokens: Mapping[str, Any], requested_scopes: list[str]) -> frozenset[str]:
        raw_scopes = parse_scopes(idp_tokens["scope"]) or [] if "scope" in idp_tokens else requested_scopes
        return frozenset(self._translate_scopes_from_idp(raw_scopes))

    def _reference_grant(self, token: str, *, expected_token_use: str) -> _ReferenceGrant:
        payload = self.jwt_issuer.verify_token(token, expected_token_use=expected_token_use)
        if not isinstance(payload, dict):
            raise TypeError("FastMCP JWT payload must be an object")
        client_id = _required_nonblank_string(payload["client_id"], field_name="client_id")
        scope = payload["scope"]
        if not isinstance(scope, str):
            raise TypeError("scope must be a string")
        jti = _required_nonblank_string(payload["jti"], field_name="jti")
        return _ReferenceGrant(
            grant_id=_grant_id_from_upstream_claims(payload["upstream_claims"]),
            client_id=client_id,
            scopes=frozenset(parse_scopes(scope) or []),
            jti=jti,
        )

    def _validate_issued_family(
        self, token: OAuthToken, *, grant_id: UUID, client_id: str, allowed_scopes: frozenset[str]
    ) -> tuple[TokenFamilyEvidence, frozenset[str]]:
        access = self._reference_grant(token.access_token, expected_token_use="access")
        response_scopes = frozenset(parse_scopes(token.scope) or [])
        if (
            access.grant_id != grant_id
            or access.client_id != client_id
            or access.scopes != response_scopes
            or not response_scopes <= allowed_scopes
        ):
            raise TokenError("invalid_grant", _INVALID_GRANT)

        refresh_jti = None
        if token.refresh_token is not None:
            refresh = self._reference_grant(token.refresh_token, expected_token_use="refresh")
            if refresh.grant_id != grant_id or refresh.client_id != client_id or refresh.scopes != response_scopes:
                raise TokenError("invalid_grant", _INVALID_GRANT)
            refresh_jti = refresh.jti
        return TokenFamilyEvidence(access_jti=access.jti, refresh_jti=refresh_jti), response_scopes


def assert_fastmcp_adapter_compatibility() -> None:
    """Fail loudly when the one-version adapter containment contract drifts."""

    if fastmcp.__version__ != _SUPPORTED_FASTMCP_VERSION:
        raise AssertionError(f"HakuAgentOAuthProxy supports FastMCP {_SUPPORTED_FASTMCP_VERSION} only")
    if HakuAgentOAuthProxy.get_routes is not OAuthProxy.get_routes:
        raise AssertionError("Haku must leave OAuthProxy.get_routes untouched")
    if HakuAgentOAuthProxy._handle_idp_callback is not OAuthProxy._handle_idp_callback:
        raise AssertionError("Haku must leave OAuthProxy._handle_idp_callback untouched")
    if HakuAgentOAuthProxy.load_authorization_code is not OAuthProxy.load_authorization_code:
        raise AssertionError("Haku must leave OAuthProxy.load_authorization_code untouched")
    if HakuAgentOAuthProxy.register_client is not OAuthProxy.register_client:
        raise AssertionError("Haku must leave OAuthProxy.register_client untouched")

    _assert_method_parameters(OAuthProxy.authorize, ("self", "client", "params"))
    _assert_method_parameters(OAuthProxy.exchange_authorization_code, ("self", "client", "authorization_code"))
    _assert_method_parameters(OAuthProxy.exchange_refresh_token, ("self", "client", "refresh_token", "scopes"))
    _assert_method_parameters(OAuthProxy.load_access_token, ("self", "token"))
    _assert_method_parameters(OAuthProxy.revoke_token, ("self", "token"))
    _assert_method_parameters(OAuthProxy._extract_upstream_claims, ("self", "idp_tokens"))
    _assert_method_parameters(OAuthProxy._translate_scopes_from_idp, ("self", "scopes"))
    _assert_method_parameters(OAuthProxy._try_transparent_refresh, ("self", "upstream_token_set"))
    _assert_method_parameters(MultiAuth.verify_token, ("self", "token"))
    if HakuFailurePreservingMultiAuth.get_routes is not MultiAuth.get_routes:
        raise AssertionError("Haku auth composite must leave MultiAuth route delegation untouched")
    if HakuFailurePreservingMultiAuth.get_well_known_routes is not MultiAuth.get_well_known_routes:
        raise AssertionError("Haku auth composite must leave MultiAuth discovery delegation untouched")


def _assert_method_parameters(method: Callable[..., Any], expected: tuple[str, ...]) -> None:
    actual = tuple(inspect.signature(method).parameters)
    if actual != expected:
        raise AssertionError(f"{method.__qualname__} signature changed: {actual!r}")


def _grant_id_from_claims(claims: Mapping[str, Any]) -> UUID:
    return _grant_id_from_upstream_claims(claims["upstream_claims"])


def _grant_id_from_upstream_claims(value: object) -> UUID:
    if not isinstance(value, dict) or set(value) != {"grant_id"}:
        raise TypeError("upstream_claims must contain only grant_id")
    raw_grant_id = _required_nonblank_string(value["grant_id"], field_name="grant_id")
    grant_id = UUID(raw_grant_id)
    if str(grant_id) != raw_grant_id:
        raise ValueError("grant_id must use canonical UUID form")
    return grant_id


def _required_client_id(client_id: str | None) -> str:
    if client_id is None or not client_id.strip():
        raise AuthorizeError("invalid_request", "Client ID is required")
    return client_id


def _required_client_id_for_token(client_id: str | None) -> str:
    if client_id is None or not client_id.strip():
        raise TokenError("invalid_client", "Client ID is required")
    return client_id


def _required_nonblank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a non-blank string")
    return value


def _validate_grant_authorization(
    authorization: GrantAuthorization, *, grant_id: UUID, client_id: str, scopes: frozenset[str]
) -> None:
    if (
        not isinstance(authorization.grant_id, UUID)
        or authorization.grant_id.int == 0
        or authorization.grant_id != grant_id
        or authorization.client_id != client_id
        or not scopes <= authorization.allowed_scopes
    ):
        raise GrantRejectedError
    _validate_agent_actor(authorization.actor)


def _validate_agent_actor(actor: object) -> AgentActor:
    if (
        not isinstance(actor, AgentActor)
        or not isinstance(actor.agent_id, UUID)
        or actor.agent_id.int == 0
        or not isinstance(actor.operator_id, UUID)
        or actor.operator_id.int == 0
        or not isinstance(actor.binding_id, UUID)
        or actor.binding_id.int == 0
    ):
        raise GrantRejectedError
    return actor


def _require_same_actor(before: GrantAuthorization, after: GrantAuthorization, *, scopes: frozenset[str]) -> None:
    _validate_grant_authorization(after, grant_id=before.grant_id, client_id=before.client_id, scopes=scopes)
    if after.actor != before.actor:
        raise GrantRejectedError


def _transient_upstream_error(error: BaseException) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code >= 500


def _service_unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=503, detail=detail, headers={"Retry-After": str(_RETRY_AFTER_SECONDS)})

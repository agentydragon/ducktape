"""Test-only feasibility adapter for Haku-owned MCP agent enrollment."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
import unicodedata
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from fastmcp.utilities.auth import parse_scopes
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from key_value.aio.protocols import AsyncKeyValue
from mcp import types as mcp_types
from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp_infra.authentik_auth.fastmcp_proxy import DownstreamClientIdentityOIDCProxy
from mcp_infra.authentik_auth.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
    VerifiedOidcPrincipal,
)

# FastMCP 3.4.4 retains its transaction for 15 minutes and downstream code for 5.
# Keeping the tuple unavailable for longer prevents an old code from binding to a
# later authorization that happens to reuse the same client, redirect, and PKCE key.
FASTMCP_TRANSACTION_TTL_SECONDS = 15 * 60
FASTMCP_CODE_TTL_SECONDS = 5 * 60
DEFAULT_TOMBSTONE_TTL_SECONDS = FASTMCP_TRANSACTION_TTL_SECONDS + FASTMCP_CODE_TTL_SECONDS + 60
DEFAULT_INTERACTION_TTL_SECONDS = 10 * 60

_INVALID_GRANT = "The agent enrollment grant is invalid."


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    issuer: str
    subject: str
    username: str
    session_id: str


@dataclass(frozen=True, slots=True)
class IssuerSubject:
    issuer: str
    subject: str


@dataclass(frozen=True, slots=True)
class CanonicalOperatorMatcher:
    """Map issuer-scoped identities onto Haku's stable Operator anchor."""

    anchors: Mapping[IssuerSubject, str]

    def browser_anchor(self, operator: OperatorIdentity) -> str | None:
        return self.anchors.get(IssuerSubject(operator.issuer, operator.subject))

    def mcp_anchor(self, principal: VerifiedOidcPrincipal) -> str | None:
        return self.anchors.get(IssuerSubject(principal.issuer, principal.subject))

    def same_operator(self, operator: OperatorIdentity, principal: VerifiedOidcPrincipal) -> str | None:
        browser = self.browser_anchor(operator)
        return browser if browser is not None and browser == self.mcp_anchor(principal) else None


@dataclass(frozen=True, slots=True)
class AuthorizationTuple:
    client_id: str
    redirect_uri: str
    code_challenge: str

    @property
    def digest(self) -> str:
        encoded = json.dumps([self.client_id, self.redirect_uri, self.code_challenge], separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AwaitingBrowser:
    nonce_digest: str


@dataclass(frozen=True, slots=True)
class AwaitingApproval:
    operator: OperatorIdentity
    csrf_digest: str


@dataclass(frozen=True, slots=True)
class AllowedEnrollment:
    operator: OperatorIdentity
    display_name: str
    normalized_name: str


@dataclass(frozen=True, slots=True)
class ExchangingEnrollment:
    operator: OperatorIdentity
    display_name: str
    normalized_name: str
    grant_id: str


class ClosedReason(StrEnum):
    DENIED = "denied"
    EXPIRED = "expired"
    IDENTITY_MISMATCH = "identity_mismatch"
    ISSUED = "issued"


@dataclass(frozen=True, slots=True)
class ClosedEnrollment:
    reason: ClosedReason


type EnrollmentPhase = AwaitingBrowser | AwaitingApproval | AllowedEnrollment | ExchangingEnrollment | ClosedEnrollment


@dataclass(frozen=True, slots=True)
class EnrollmentInteraction:
    locator: str
    correlation: AuthorizationTuple
    upstream_url: str
    client_name: str
    requested_scopes: frozenset[str]
    expires_at: float
    phase: EnrollmentPhase


@dataclass(frozen=True, slots=True)
class PendingNameOwner:
    interaction_locator: str


@dataclass(frozen=True, slots=True)
class ActiveNameOwner:
    agent_id: str


type NameOwner = PendingNameOwner | ActiveNameOwner


@dataclass(frozen=True, slots=True)
class Agent:
    agent_id: str
    display_name: str
    normalized_name: str


@dataclass(frozen=True, slots=True)
class GrantCore:
    grant_id: str
    agent_id: str
    client_id: str
    allowed_scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class TokenFamilyEvidence:
    """Receipt for initial issuance/reconciliation, not current token-family state."""

    access_jti: str
    refresh_jti: str | None


@dataclass(frozen=True, slots=True)
class IssuingGrant:
    core: GrantCore
    evidence: TokenFamilyEvidence | None = None


@dataclass(frozen=True, slots=True)
class IssuedGrant:
    core: GrantCore
    evidence: TokenFamilyEvidence


@dataclass(frozen=True, slots=True)
class ActiveGrant:
    core: GrantCore
    evidence: TokenFamilyEvidence


@dataclass(frozen=True, slots=True)
class RevokedGrant:
    core: GrantCore


type Grant = IssuingGrant | IssuedGrant | ActiveGrant | RevokedGrant


@dataclass(frozen=True, slots=True)
class IssueContext:
    grant_id: str
    allowed_scopes: frozenset[str]


_ISSUE_CONTEXT: ContextVar[IssueContext | None] = ContextVar("haku_agent_issue_context", default=None)


class DuplicateAuthorizationError(Exception):
    pass


class EnrollmentRejectedError(Exception):
    pass


class ExchangeAlreadyClaimedError(Exception):
    pass


class EnrollmentExpiredError(Exception):
    pass


class NameUnavailableError(Exception):
    pass


class GrantDispatchRejectedError(Exception):
    pass


def _digest_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _normalized_agent_name(display_name: str) -> tuple[str, str]:
    presentation = unicodedata.normalize("NFC", display_name)
    forbidden_bidi = {"\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069"}
    if any(unicodedata.category(character) == "Cc" or character in forbidden_bidi for character in presentation):
        raise ValueError("agent name must not contain control or bidirectional formatting characters")
    display = " ".join(presentation.split())
    if not display:
        raise ValueError("agent name must not be empty")
    if len(display) > 80:
        raise ValueError("agent name must contain at most 80 Unicode scalars")
    return display, unicodedata.normalize("NFKC", display).casefold()


def _one_form_value(body: bytes, key: str) -> str:
    try:
        values = parse_qs(body.decode(), strict_parsing=True)[key]
    except (KeyError, UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=f"invalid {key}") from error
    if len(values) != 1:
        raise HTTPException(status_code=400, detail=f"invalid {key}")
    return values[0]


class InMemoryAgentEnrollmentStore:
    """Atomic behavioral model for the mounted spike.

    ``_name_owners`` is the proof's single global unique-reservation index;
    Agent display fields are a convenient joined view, not a second production
    record. P5 uses the relational ownership model in the architecture plan.
    """

    def __init__(
        self,
        *,
        public_origin: str,
        clock: Callable[[], float] = time.time,
        interaction_ttl_seconds: int = DEFAULT_INTERACTION_TTL_SECONDS,
        tombstone_ttl_seconds: int = DEFAULT_TOMBSTONE_TTL_SECONDS,
    ) -> None:
        if tombstone_ttl_seconds <= FASTMCP_TRANSACTION_TTL_SECONDS + FASTMCP_CODE_TTL_SECONDS:
            raise ValueError("tuple tombstones must outlive FastMCP transaction plus code TTL")
        parsed_origin = urlsplit(public_origin)
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not parsed_origin.netloc
            or parsed_origin.path not in {"", "/"}
        ):
            raise ValueError("public_origin must contain only scheme and authority")
        self.public_origin = public_origin.rstrip("/")
        self._clock = clock
        self._interaction_ttl_seconds = interaction_ttl_seconds
        self._tombstone_ttl_seconds = tombstone_ttl_seconds
        self._lock = asyncio.Lock()
        self._interactions: dict[str, EnrollmentInteraction] = {}
        self._live_correlations: dict[str, str] = {}
        self._tombstones: dict[str, float] = {}
        self._name_owners: dict[str, NameOwner] = {}
        self._agents: dict[str, Agent] = {}
        self._grants: dict[str, Grant] = {}
        self.fail_next_issue_completion = False

    async def reserve(
        self, *, correlation: AuthorizationTuple, upstream_url: str, client_name: str, requested_scopes: frozenset[str]
    ) -> tuple[EnrollmentInteraction, str]:
        browser_nonce = secrets.token_urlsafe(32)
        now = self._clock()
        async with self._lock:
            self._expire_locked(now)
            digest = correlation.digest
            if digest in self._live_correlations or self._tombstones.get(digest, 0) > now:
                raise DuplicateAuthorizationError
            locator = secrets.token_urlsafe(32)
            interaction = EnrollmentInteraction(
                locator=locator,
                correlation=correlation,
                upstream_url=upstream_url,
                client_name=client_name,
                requested_scopes=requested_scopes,
                expires_at=now + self._interaction_ttl_seconds,
                phase=AwaitingBrowser(nonce_digest=_digest_secret(browser_nonce)),
            )
            self._interactions[locator] = interaction
            self._live_correlations[digest] = locator
            self._tombstones[digest] = now + self._tombstone_ttl_seconds
            return interaction, browser_nonce

    async def open_page(
        self, *, locator: str, browser_nonce: str, operator: OperatorIdentity
    ) -> tuple[EnrollmentInteraction, str]:
        csrf = secrets.token_urlsafe(32)
        async with self._lock:
            interaction = self._live_interaction_locked(locator)
            if not isinstance(interaction.phase, AwaitingBrowser) or not hmac.compare_digest(
                interaction.phase.nonce_digest, _digest_secret(browser_nonce)
            ):
                raise EnrollmentRejectedError
            opened = replace(interaction, phase=AwaitingApproval(operator=operator, csrf_digest=_digest_secret(csrf)))
            self._interactions[locator] = opened
            return opened, csrf

    async def decide(
        self, *, locator: str, operator: OperatorIdentity, csrf: str, approve: bool, display_name: str
    ) -> str | None:
        async with self._lock:
            interaction = self._live_interaction_locked(locator)
            phase = interaction.phase
            if (
                not isinstance(phase, AwaitingApproval)
                or phase.operator.session_id != operator.session_id
                or phase.operator.issuer != operator.issuer
                or phase.operator.subject != operator.subject
                or not hmac.compare_digest(phase.csrf_digest, _digest_secret(csrf))
            ):
                raise EnrollmentRejectedError
            if not approve:
                self._close_interaction_locked(interaction, ClosedReason.DENIED)
                return None
            display, normalized = _normalized_agent_name(display_name)
            if normalized in self._name_owners:
                raise NameUnavailableError
            self._name_owners[normalized] = PendingNameOwner(interaction_locator=locator)
            self._interactions[locator] = replace(
                interaction,
                phase=AllowedEnrollment(operator=operator, display_name=display, normalized_name=normalized),
            )
            return interaction.upstream_url

    async def begin_exchange(
        self,
        *,
        correlation: AuthorizationTuple,
        client_id: str,
        principal: VerifiedOidcPrincipal,
        matcher: CanonicalOperatorMatcher,
        granted_scopes: frozenset[str],
    ) -> GrantCore:
        async with self._lock:
            locator = self._live_correlations.get(correlation.digest)
            interaction = self._live_interaction_locked(locator) if locator is not None else None
            if (
                interaction is not None
                and interaction.correlation == correlation
                and isinstance(interaction.phase, ExchangingEnrollment)
            ):
                raise ExchangeAlreadyClaimedError
            if (
                interaction is None
                or interaction.correlation != correlation
                or not isinstance(interaction.phase, AllowedEnrollment)
            ):
                raise EnrollmentRejectedError
            phase = interaction.phase
            if matcher.same_operator(phase.operator, principal) is None or not granted_scopes <= (
                interaction.requested_scopes
            ):
                self._release_pending_name_locked(phase.normalized_name, interaction.locator)
                self._close_interaction_locked(interaction, ClosedReason.IDENTITY_MISMATCH)
                raise EnrollmentRejectedError
            agent_id = secrets.token_urlsafe(24)
            grant_id = secrets.token_urlsafe(24)
            agent = Agent(agent_id=agent_id, display_name=phase.display_name, normalized_name=phase.normalized_name)
            core = GrantCore(grant_id=grant_id, agent_id=agent_id, client_id=client_id, allowed_scopes=granted_scopes)
            self._agents[agent_id] = agent
            self._name_owners[phase.normalized_name] = ActiveNameOwner(agent_id=agent_id)
            self._grants[grant_id] = IssuingGrant(core=core)
            self._interactions[interaction.locator] = replace(
                interaction,
                phase=ExchangingEnrollment(
                    operator=phase.operator,
                    display_name=phase.display_name,
                    normalized_name=phase.normalized_name,
                    grant_id=grant_id,
                ),
            )
            return core

    async def record_token_family(self, grant_id: str, evidence: TokenFamilyEvidence) -> None:
        async with self._lock:
            grant = self._grants.get(grant_id)
            if not isinstance(grant, IssuingGrant):
                raise EnrollmentRejectedError
            self._grants[grant_id] = IssuingGrant(core=grant.core, evidence=evidence)
            if self.fail_next_issue_completion:
                self.fail_next_issue_completion = False
                raise RuntimeError("injected Haku issue-transition failure")
            self._grants[grant_id] = IssuedGrant(core=grant.core, evidence=evidence)
            self._close_grant_interaction_locked(grant_id)

    async def reconcile_issuing(self, grant_id: str) -> None:
        async with self._lock:
            grant = self._grants.get(grant_id)
            if not isinstance(grant, IssuingGrant) or grant.evidence is None:
                raise EnrollmentRejectedError
            self._grants[grant_id] = IssuedGrant(core=grant.core, evidence=grant.evidence)
            self._close_grant_interaction_locked(grant_id)

    async def grant_for_refresh(self, *, grant_id: str, client_id: str, requested_scopes: frozenset[str]) -> GrantCore:
        async with self._lock:
            grant = self._grants.get(grant_id)
            if not isinstance(grant, IssuedGrant | ActiveGrant) or grant.core.client_id != client_id:
                raise EnrollmentRejectedError
            if not requested_scopes <= grant.core.allowed_scopes:
                raise EnrollmentRejectedError
            return grant.core

    async def activate_for_tool_call(self, *, grant_id: str, client_id: str, token_scopes: frozenset[str]) -> GrantCore:
        async with self._lock:
            grant = self._grants.get(grant_id)
            if (
                isinstance(grant, IssuedGrant)
                and grant.core.client_id == client_id
                and token_scopes <= grant.core.allowed_scopes
            ):
                self._grants[grant_id] = ActiveGrant(core=grant.core, evidence=grant.evidence)
                return grant.core
            if (
                isinstance(grant, ActiveGrant)
                and grant.core.client_id == client_id
                and token_scopes <= grant.core.allowed_scopes
            ):
                return grant.core
            raise GrantDispatchRejectedError

    async def revoke(self, grant_id: str) -> None:
        async with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None:
                raise EnrollmentRejectedError
            self._grants[grant_id] = RevokedGrant(core=grant.core)

    async def expire(self) -> None:
        async with self._lock:
            self._expire_locked(self._clock())

    def interactions(self) -> tuple[EnrollmentInteraction, ...]:
        return tuple(self._interactions.values())

    def agents(self) -> tuple[Agent, ...]:
        return tuple(self._agents.values())

    def grants(self) -> tuple[Grant, ...]:
        return tuple(self._grants.values())

    def grant(self, grant_id: str) -> Grant:
        return self._grants[grant_id]

    def _live_interaction_locked(self, locator: str) -> EnrollmentInteraction:
        interaction = self._interactions.get(locator)
        if interaction is None:
            raise EnrollmentRejectedError
        if interaction.expires_at <= self._clock():
            phase = interaction.phase
            if isinstance(phase, AllowedEnrollment):
                self._release_pending_name_locked(phase.normalized_name, locator)
            self._close_interaction_locked(interaction, ClosedReason.EXPIRED)
            raise EnrollmentExpiredError
        return interaction

    def _expire_locked(self, now: float) -> None:
        for interaction in tuple(self._interactions.values()):
            if interaction.expires_at > now or isinstance(interaction.phase, ClosedEnrollment):
                continue
            if isinstance(interaction.phase, AllowedEnrollment):
                self._release_pending_name_locked(interaction.phase.normalized_name, interaction.locator)
            self._close_interaction_locked(interaction, ClosedReason.EXPIRED)
        self._tombstones = {digest: expiry for digest, expiry in self._tombstones.items() if expiry > now}

    def _release_pending_name_locked(self, normalized_name: str, locator: str) -> None:
        owner = self._name_owners.get(normalized_name)
        if isinstance(owner, PendingNameOwner) and owner.interaction_locator == locator:
            del self._name_owners[normalized_name]

    def _close_interaction_locked(self, interaction: EnrollmentInteraction, reason: ClosedReason) -> None:
        self._interactions[interaction.locator] = replace(interaction, phase=ClosedEnrollment(reason=reason))
        self._live_correlations.pop(interaction.correlation.digest, None)

    def _close_grant_interaction_locked(self, grant_id: str) -> None:
        for interaction in self._interactions.values():
            if isinstance(interaction.phase, ExchangingEnrollment) and interaction.phase.grant_id == grant_id:
                self._close_interaction_locked(interaction, ClosedReason.ISSUED)
                return
        raise EnrollmentRejectedError


class AgentEnrollmentOIDCProxy(DownstreamClientIdentityOIDCProxy):
    """Compose Haku enrollment around FastMCP's public OAuth state machine."""

    def __init__(
        self,
        *,
        config_url: str,
        client_id: str,
        client_secret: str,
        base_url: str,
        client_storage: AsyncKeyValue,
        expected_issuer: str,
        enrollment_store: InMemoryAgentEnrollmentStore,
        operator_matcher: CanonicalOperatorMatcher,
    ) -> None:
        super().__init__(
            config_url=config_url,
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            require_authorization_consent="external",
            client_storage=client_storage,
        )
        self._enrollment_store = enrollment_store
        self._operator_matcher = operator_matcher
        self._principal_resolver = AuthentikOidcPrincipalResolver(
            expected_issuer=expected_issuer,
            discovered_issuer=str(self.oidc_config.issuer) if self.oidc_config.issuer is not None else None,
            jwks_uri=str(self.oidc_config.jwks_uri) if self.oidc_config.jwks_uri is not None else None,
            signing_algorithms=self.oidc_config.id_token_signing_alg_values_supported,
            client_id=client_id,
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if client.client_id is None:
            raise AuthorizeError("invalid_request", "Client ID is required")
        # The mounted SDK AuthorizationHandler has already validated the registered
        # redirect, scope, and Literal["S256"] method before creating these public params.
        upstream_url = await super().authorize(client, params)
        correlation = AuthorizationTuple(
            client_id=client.client_id, redirect_uri=str(params.redirect_uri), code_challenge=params.code_challenge
        )
        try:
            interaction, browser_nonce = await self._enrollment_store.reserve(
                correlation=correlation,
                upstream_url=upstream_url,
                client_name=client.client_name or client.client_id,
                requested_scopes=frozenset(params.scopes or []),
            )
        except DuplicateAuthorizationError:
            # super() may have created an unreachable transaction. It expires on
            # FastMCP's own 15-minute TTL; no state or winning URL is disclosed.
            raise AuthorizeError("temporarily_unavailable", "An equivalent authorization is already pending") from None
        return (
            f"{self._enrollment_store.public_origin}/agent-enrollment/{interaction.locator}?"
            f"{urlencode({'browser_nonce': browser_nonce})}"
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code_model = await self._code_store.get(key=authorization_code.code)
        if code_model is None or client.client_id is None or code_model.client_id != client.client_id:
            raise TokenError("invalid_grant", _INVALID_GRANT)
        correlation = AuthorizationTuple(
            client_id=client.client_id,
            redirect_uri=str(authorization_code.redirect_uri),
            code_challenge=authorization_code.code_challenge,
        )
        try:
            principal = await self._principal_resolver.resolve(code_model.idp_tokens)
            # Mirror FastMCP 3.4.4's effective client-facing scope calculation
            # exactly. Raw IdP-wire scopes belong to a different domain and may
            # require provider-specific translation before they can govern Haku.
            granted_scopes = (
                frozenset(self._translate_scopes_from_idp(parse_scopes(code_model.idp_tokens["scope"]) or []))
                if "scope" in code_model.idp_tokens
                else frozenset(authorization_code.scopes)
            )
            core = await self._enrollment_store.begin_exchange(
                correlation=correlation,
                client_id=client.client_id,
                principal=principal,
                matcher=self._operator_matcher,
                granted_scopes=granted_scopes,
            )
        except (InvalidOidcPrincipalError, EnrollmentRejectedError):
            await self._code_store.delete(key=authorization_code.code)
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except ExchangeAlreadyClaimedError:
            # Another request won the Haku CAS and may not yet have read the
            # FastMCP code. The loser must not delete that winner's code.
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        except OidcPrincipalVerificationUnavailableError:
            raise HTTPException(
                status_code=503,
                detail="Upstream identity verification temporarily unavailable",
                headers={"Retry-After": "60"},
            ) from None

        context_token = _ISSUE_CONTEXT.set(IssueContext(grant_id=core.grant_id, allowed_scopes=core.allowed_scopes))
        try:
            token = await super().exchange_authorization_code(client, authorization_code)
        finally:
            _ISSUE_CONTEXT.reset(context_token)
        evidence = self._token_family_evidence(token)
        await self._enrollment_store.record_token_family(core.grant_id, evidence)
        return token

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")
        try:
            claims = self.jwt_issuer.verify_token(refresh_token.token, expected_token_use="refresh")
            upstream_claims = claims["upstream_claims"]
            grant_id = upstream_claims["grant_id"]
            if not isinstance(grant_id, str):
                raise TypeError("grant_id is not a string")
            core = await self._enrollment_store.grant_for_refresh(
                grant_id=grant_id, client_id=client.client_id, requested_scopes=frozenset(scopes)
            )
        except (EnrollmentRejectedError, KeyError, TypeError, ValueError):
            raise TokenError("invalid_grant", _INVALID_GRANT) from None
        context_token = _ISSUE_CONTEXT.set(IssueContext(grant_id=core.grant_id, allowed_scopes=core.allowed_scopes))
        try:
            return await super().exchange_refresh_token(client, refresh_token, scopes)
        finally:
            _ISSUE_CONTEXT.reset(context_token)

    def _translate_scopes_from_idp(self, scopes: list[str]) -> list[str]:
        translated = super()._translate_scopes_from_idp(scopes)
        context = _ISSUE_CONTEXT.get()
        if context is not None and not frozenset(translated) <= context.allowed_scopes:
            raise TokenError("invalid_scope", "The upstream provider broadened the approved scopes")
        return translated

    async def _extract_upstream_claims(self, idp_tokens: dict[str, object]) -> dict[str, object] | None:
        context = _ISSUE_CONTEXT.get()
        if context is None:
            raise TokenError("invalid_grant", _INVALID_GRANT)
        return {"grant_id": context.grant_id}

    def _token_family_evidence(self, token: OAuthToken) -> TokenFamilyEvidence:
        access = self.jwt_issuer.verify_token(token.access_token, expected_token_use="access")
        refresh_jti = None
        if token.refresh_token is not None:
            refresh = self.jwt_issuer.verify_token(token.refresh_token, expected_token_use="refresh")
            refresh_jti = str(refresh["jti"])
        return TokenFamilyEvidence(access_jti=str(access["jti"]), refresh_jti=refresh_jti)


def build_agent_enrollment_router(
    *,
    store: InMemoryAgentEnrollmentStore,
    operator_from_request: Callable[[Request], OperatorIdentity | None],
    login_path: str,
) -> APIRouter:
    """Build the test-only Haku route that owns naming and operator approval."""

    router = APIRouter()
    templates = Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=select_autoescape(enabled_extensions=("html", "j2"), default_for_string=True, default=True),
        undefined=StrictUndefined,
    )

    @router.get("/agent-enrollment/{locator}")
    async def enrollment_page(request: Request, locator: str, browser_nonce: str) -> Response:
        operator = operator_from_request(request)
        if operator is None:
            return RedirectResponse(url=f"{login_path}?{urlencode({'return_to': str(request.url)})}", status_code=303)
        try:
            interaction, csrf = await store.open_page(locator=locator, browser_nonce=browser_nonce, operator=operator)
        except EnrollmentExpiredError:
            raise HTTPException(status_code=410, detail="enrollment expired") from None
        except EnrollmentRejectedError:
            raise HTTPException(status_code=403, detail="enrollment session mismatch") from None
        html = templates.get_template("mcp_agent_enrollment_spike.html.j2").render(
            locator=locator,
            csrf=csrf,
            client_name=interaction.client_name,
            redirect_host=urlsplit(interaction.correlation.redirect_uri).hostname,
            scopes=sorted(interaction.requested_scopes),
            operator_username=operator.username,
        )
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; form-action 'self'; base-uri 'none'; object-src 'none'; "
                    "script-src 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
            },
        )

    @router.post("/agent-enrollment/{locator}")
    async def enrollment_decision(request: Request, locator: str) -> Response:
        operator = operator_from_request(request)
        if operator is None:
            raise HTTPException(status_code=401, detail="operator authentication required")
        if request.headers.get("origin") != store.public_origin:
            raise HTTPException(status_code=403, detail="invalid origin")
        body = await request.body()
        action = _one_form_value(body, "action")
        if action not in {"approve", "deny"}:
            raise HTTPException(status_code=400, detail="invalid action")
        try:
            upstream_url = await store.decide(
                locator=locator,
                operator=operator,
                csrf=_one_form_value(body, "csrf"),
                approve=action == "approve",
                display_name=_one_form_value(body, "agent_name") if action == "approve" else "denied",
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except NameUnavailableError:
            raise HTTPException(status_code=409, detail="agent name is already in use") from None
        except EnrollmentExpiredError:
            raise HTTPException(status_code=410, detail="enrollment expired") from None
        except EnrollmentRejectedError:
            raise HTTPException(status_code=403, detail="enrollment session mismatch") from None
        if upstream_url is None:
            return HTMLResponse("Enrollment denied.", status_code=200)
        return RedirectResponse(url=upstream_url, status_code=303)

    return router


class AgentGrantMiddleware(Middleware):
    """Activate an issued grant at the actual tools/call perimeter."""

    def __init__(self, store: InMemoryAgentEnrollmentStore) -> None:
        self._store = store
        self.dispatched_tools: list[str] = []

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        token = get_access_token()
        if token is None:
            raise ToolError("agent grant is missing")
        try:
            upstream_claims = token.claims["upstream_claims"]
            grant_id = upstream_claims["grant_id"]
            if not isinstance(grant_id, str):
                raise TypeError("grant_id is not a string")
            await self._store.activate_for_tool_call(
                grant_id=grant_id, client_id=token.client_id, token_scopes=frozenset(token.scopes)
            )
        except (GrantDispatchRejectedError, KeyError, TypeError):
            raise ToolError("agent grant is not active") from None
        self.dispatched_tools.append(context.message.name)
        return await call_next(context)


def assert_fastmcp_enrollment_compatibility() -> None:
    """Pin the intentionally narrow FastMCP extension surface used by the spike."""

    if AgentEnrollmentOIDCProxy.get_routes is not OAuthProxy.get_routes:
        raise AssertionError("the spike must leave OAuth routes untouched")
    if AgentEnrollmentOIDCProxy._handle_idp_callback is not OAuthProxy._handle_idp_callback:
        raise AssertionError("the spike must leave the IdP callback untouched")
    if AgentEnrollmentOIDCProxy.load_authorization_code is not OAuthProxy.load_authorization_code:
        raise AssertionError("the spike must leave authorization-code loading untouched")
    if AgentEnrollmentOIDCProxy.register_client is not OAuthProxy.register_client:
        raise AssertionError("the spike must leave DCR untouched")

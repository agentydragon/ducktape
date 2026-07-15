"""Contract tests for Haku's bounded FastMCP Agent adapter."""

from __future__ import annotations

import inspect
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, MultiAuth, TokenVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from mcp.server.auth.provider import AuthorizeError, RefreshToken, TokenError
from mcp.shared.auth import OAuthToken
from starlette.exceptions import HTTPException

from haku.console.mcp_auth.fastmcp_adapter import (
    AgentActorAuthorityUnavailableError,
    AgentActorResolutionUnavailableError,
    AgentGrantAuthorityUnavailableError,
    AuthorizationCorrelation,
    AuthorizationRequest,
    BearerFailureObservingKeyValue,
    BearerVerificationUnavailableError,
    ClientSoftwareSnapshot,
    DuplicateAuthorizationError,
    EnrollmentRejectedError,
    ExchangeAlreadyClaimedError,
    FailureObservingJWTVerifier,
    GrantAuthorization,
    GrantRejectedError,
    HakuAgentGrantMiddleware,
    HakuAgentOAuthProxy,
    HakuFailurePreservingMultiAuth,
    StaticAgentActorResolver,
    TokenFamilyEvidence,
    assert_fastmcp_adapter_compatibility,
    current_grant_request_context,
    get_agent_actor,
    observe_bearer_operational_failure,
)
from haku.console.tool_call_actor import AgentActor
from mcp_infra.authentik_auth.fastmcp_proxy import RetryableRefreshOIDCProxy
from mcp_infra.authentik_auth.oidc_principal import (
    AuthentikOidcPrincipalResolver,
    InvalidOidcPrincipalError,
    OidcPrincipalVerificationUnavailableError,
    VerifiedOidcPrincipal,
)

GRANT_ID = UUID("10000000-0000-4000-8000-000000000001")
BINDING_ID = UUID("20000000-0000-4000-8000-000000000002")
AGENT_ID = UUID("30000000-0000-4000-8000-000000000003")
OPERATOR_ID = UUID("40000000-0000-4000-8000-000000000004")
CLIENT_ID = "client-software-id"
REDIRECT_URI = "https://client.example.test/callback"
CODE_CHALLENGE = "pkce-challenge"
SCOPES = frozenset({"read"})


class _TestVerifier(TokenVerifier):
    def __init__(self, *, result: AccessToken | None = None, failure: Exception | None = None) -> None:
        super().__init__()
        self.result = result
        self.failure = failure
        self.calls: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        self.calls.append(token)
        if self.failure is not None:
            raise self.failure
        return self.result


def _authorization(*, scopes: frozenset[str] = SCOPES) -> GrantAuthorization:
    return GrantAuthorization(
        grant_id=GRANT_ID,
        actor=AgentActor(agent_id=AGENT_ID, operator_id=OPERATOR_ID, binding_id=BINDING_ID),
        client_id=CLIENT_ID,
        allowed_scopes=scopes,
    )


def _authority() -> SimpleNamespace:
    return SimpleNamespace(
        reserve_authorization=AsyncMock(return_value="https://haku.example.test/agent-enrollment/interaction"),
        begin_exchange=AsyncMock(return_value=_authorization()),
        record_token_family=AsyncMock(),
        resolve_grant=AsyncMock(return_value=_authorization()),
        activate_for_tool_call=AsyncMock(return_value=_authorization()),
        revoke_grant=AsyncMock(),
    )


def _principal_resolver() -> SimpleNamespace:
    return SimpleNamespace(
        resolve=AsyncMock(
            return_value=VerifiedOidcPrincipal(
                issuer="https://auth.example.test/application/o/haku-mcp/", subject="operator-subject"
            )
        )
    )


def _bare_proxy(
    *, authority: SimpleNamespace | None = None, resolver: SimpleNamespace | None = None
) -> HakuAgentOAuthProxy:
    proxy = object.__new__(HakuAgentOAuthProxy)
    state = cast(Any, proxy)
    state._grant_authority = authority or _authority()
    state._principal_resolver = resolver or _principal_resolver()
    state._code_store = AsyncMock()
    state._jwt_issuer = Mock()
    return proxy


def _client() -> SimpleNamespace:
    # FastMCP's synthetic CIMD client deliberately keeps redirect_uris=None
    # after validating the requested URI against its fetched document.
    return SimpleNamespace(client_id=CLIENT_ID, client_name="Claude", redirect_uris=None)


def _params() -> SimpleNamespace:
    return SimpleNamespace(redirect_uri=REDIRECT_URI, code_challenge=CODE_CHALLENGE, scopes=["read"])


def _code() -> SimpleNamespace:
    return SimpleNamespace(
        code="authorization-code",
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_challenge=CODE_CHALLENGE,
        scopes=["read"],
    )


def _payload(*, jti: str, token_use: str = "access", scopes: str = "read") -> dict[str, object]:
    payload: dict[str, object] = {
        "client_id": CLIENT_ID,
        "scope": scopes,
        "jti": jti,
        "upstream_claims": {"grant_id": str(GRANT_ID)},
    }
    if token_use == "refresh":
        payload["token_use"] = "refresh"
    return payload


def _install_token_payloads(proxy: HakuAgentOAuthProxy, payloads: dict[str, dict[str, object]]) -> None:
    state = cast(Any, proxy)

    def verify(token: str, expected_token_use: str = "access") -> dict[str, object]:
        payload = payloads[token]
        token_use = payload.get("token_use", "access")
        if token_use != expected_token_use:
            raise ValueError("wrong token use")
        return payload

    state._jwt_issuer.verify_token.side_effect = verify


def _family(*, scopes: str = "read") -> OAuthToken:
    return OAuthToken(
        access_token="new-access", token_type="Bearer", expires_in=3600, refresh_token="new-refresh", scope=scopes
    )


def test_proxy_builds_principal_resolver_from_fastmcp_discovery() -> None:
    oidc_config = SimpleNamespace(
        issuer="https://auth.example.test/application/o/haku/",
        jwks_uri="https://auth.example.test/application/o/haku/jwks/",
        id_token_signing_alg_values_supported=["RS256"],
    )
    client_storage = object()

    constructor: dict[str, object] = {}

    def initialize(proxy: HakuAgentOAuthProxy, **kwargs: object) -> None:
        constructor.update(kwargs)
        assert kwargs["config_url"] == "https://auth.example.test/application/o/haku/.well-known/openid-configuration"
        cast(Any, proxy).oidc_config = oidc_config

    resolver = Mock(spec=AuthentikOidcPrincipalResolver)
    with (
        patch.object(RetryableRefreshOIDCProxy, "__init__", new=initialize),
        patch(
            "haku.console.mcp_auth.fastmcp_adapter.AuthentikOidcPrincipalResolver", return_value=resolver
        ) as resolver_type,
    ):
        proxy = HakuAgentOAuthProxy(
            config_url="https://auth.example.test/application/o/haku/.well-known/openid-configuration",
            client_id="upstream-client",
            client_secret="upstream-secret",
            base_url="https://haku.example.test/",
            resource_base_url="https://haku.example.test/",
            client_storage=cast(Any, client_storage),
            expected_issuer="https://auth.example.test/application/o/haku/",
            grant_authority=cast(Any, _authority()),
        )

    resolver_type.assert_called_once_with(
        expected_issuer="https://auth.example.test/application/o/haku/",
        discovered_issuer="https://auth.example.test/application/o/haku/",
        jwks_uri="https://auth.example.test/application/o/haku/jwks/",
        signing_algorithms=["RS256"],
        client_id="upstream-client",
    )
    storage = cast(BearerFailureObservingKeyValue, constructor["client_storage"])
    assert cast(Any, storage).key_value is client_storage
    assert constructor["resource_base_url"] == "https://haku.example.test/"
    assert "token_verifier" not in constructor
    assert cast(Any, proxy)._principal_resolver is resolver


def test_proxy_verifier_hook_preserves_fastmcp_configuration() -> None:
    proxy = object.__new__(HakuAgentOAuthProxy)
    cast(Any, proxy).oidc_config = SimpleNamespace(
        issuer="https://auth.example.test/application/o/haku/",
        jwks_uri="https://auth.example.test/application/o/haku/jwks/",
    )

    verifier = proxy.get_token_verifier(
        algorithm="ES256", audience="api-audience", required_scopes=["read"], timeout_seconds=13
    )

    assert isinstance(verifier, FailureObservingJWTVerifier)
    assert verifier.jwks_uri == "https://auth.example.test/application/o/haku/jwks/"
    assert verifier.issuer == "https://auth.example.test/application/o/haku/"
    assert verifier.algorithm == "ES256"
    assert verifier.audience == "api-audience"
    assert verifier.required_scopes == ["read"]


async def test_authorize_uses_validated_correlation_when_cimd_client_omits_redirect_list() -> None:
    authority = _authority()
    proxy = _bare_proxy(authority=authority)
    events: list[str] = []

    async def fastmcp_authorize(self: object, client: object, params: object) -> str:
        _ = self, client, params
        events.append("fastmcp-authorize")
        return "https://auth.example.test/authorize?opaque=1"

    async def reserve(**kwargs: object) -> str:
        events.append("haku-reserve")
        request = cast(AuthorizationRequest, kwargs["request"])
        assert request == AuthorizationRequest(
            correlation=AuthorizationCorrelation(CLIENT_ID, REDIRECT_URI, CODE_CHALLENGE),
            client=ClientSoftwareSnapshot(CLIENT_ID, "Claude"),
            requested_scopes=SCOPES,
        )
        assert kwargs["upstream_authorization_url"] == "https://auth.example.test/authorize?opaque=1"
        return "https://haku.example.test/agent-enrollment/interaction"

    authority.reserve_authorization.side_effect = reserve
    with patch.object(RetryableRefreshOIDCProxy, "authorize", new=fastmcp_authorize):
        result = await proxy.authorize(cast(Any, _client()), cast(Any, _params()))

    assert result == "https://haku.example.test/agent-enrollment/interaction"
    assert events == ["fastmcp-authorize", "haku-reserve"]


@pytest.mark.parametrize("failure", [DuplicateAuthorizationError(), AgentGrantAuthorityUnavailableError()])
async def test_authorize_classifies_reservation_failures(failure: Exception) -> None:
    authority = _authority()
    authority.reserve_authorization.side_effect = failure
    proxy = _bare_proxy(authority=authority)

    with (
        patch.object(
            RetryableRefreshOIDCProxy, "authorize", new=AsyncMock(return_value="https://auth.example.test/authorize")
        ),
        pytest.raises(AuthorizeError) as exc_info,
    ):
        await proxy.authorize(cast(Any, _client()), cast(Any, _params()))
    assert exc_info.value.error == "temporarily_unavailable"


async def test_code_exchange_creates_grant_context_and_records_family_receipt() -> None:
    authority = _authority()
    resolver = _principal_resolver()
    proxy = _bare_proxy(authority=authority, resolver=resolver)
    state = cast(Any, proxy)
    idp_tokens = {"access_token": "upstream", "token_type": "Bearer", "scope": "read"}
    state._code_store.get.return_value = SimpleNamespace(client_id=CLIENT_ID, idp_tokens=idp_tokens)
    _install_token_payloads(
        proxy,
        {"new-access": _payload(jti="access-jti"), "new-refresh": _payload(jti="refresh-jti", token_use="refresh")},
    )

    async def fastmcp_exchange(self: HakuAgentOAuthProxy, client: object, authorization_code: object) -> OAuthToken:
        _ = client
        assert await self._extract_upstream_claims(idp_tokens) == {"grant_id": str(GRANT_ID)}
        assert self._translate_scopes_from_idp(["read"]) == ["read"]
        await cast(Any, self)._code_store.delete(key=cast(Any, authorization_code).code)
        return _family()

    with patch.object(RetryableRefreshOIDCProxy, "exchange_authorization_code", new=fastmcp_exchange):
        token = await proxy.exchange_authorization_code(cast(Any, _client()), cast(Any, _code()))

    assert token.access_token == "new-access"
    resolver.resolve.assert_awaited_once_with(idp_tokens)
    authority.begin_exchange.assert_awaited_once_with(
        correlation=AuthorizationCorrelation(CLIENT_ID, REDIRECT_URI, CODE_CHALLENGE),
        client=ClientSoftwareSnapshot(CLIENT_ID, "Claude"),
        principal=resolver.resolve.return_value,
        granted_scopes=SCOPES,
    )
    authority.record_token_family.assert_awaited_once_with(
        grant_id=GRANT_ID, evidence=TokenFamilyEvidence(access_jti="access-jti", refresh_jti="refresh-jti")
    )


async def test_code_exchange_resets_issuance_context_when_fastmcp_raises() -> None:
    proxy = _bare_proxy()
    state = cast(Any, proxy)
    idp_tokens = {"access_token": "upstream", "token_type": "Bearer", "scope": "read"}
    state._code_store.get.return_value = SimpleNamespace(client_id=CLIENT_ID, idp_tokens=idp_tokens)

    async def fail_during_issuance(self: HakuAgentOAuthProxy, client: object, authorization_code: object) -> OAuthToken:
        _ = client, authorization_code
        assert await self._extract_upstream_claims(idp_tokens) == {"grant_id": str(GRANT_ID)}
        raise RuntimeError("FastMCP issuance failed")

    with (
        patch.object(RetryableRefreshOIDCProxy, "exchange_authorization_code", new=fail_during_issuance),
        pytest.raises(RuntimeError, match="issuance failed"),
    ):
        await proxy.exchange_authorization_code(cast(Any, _client()), cast(Any, _code()))

    with pytest.raises(TokenError) as exc_info:
        await proxy._extract_upstream_claims(idp_tokens)
    assert exc_info.value.error == "invalid_grant"


@pytest.mark.parametrize("failure", [InvalidOidcPrincipalError(), EnrollmentRejectedError()])
async def test_terminal_code_exchange_failure_consumes_owned_fastmcp_code(failure: Exception) -> None:
    authority = _authority()
    resolver = _principal_resolver()
    proxy = _bare_proxy(authority=authority, resolver=resolver)
    state = cast(Any, proxy)
    state._code_store.get.return_value = SimpleNamespace(
        client_id=CLIENT_ID, idp_tokens={"access_token": "upstream", "token_type": "Bearer"}
    )
    if isinstance(failure, InvalidOidcPrincipalError):
        resolver.resolve.side_effect = failure
    else:
        authority.begin_exchange.side_effect = failure

    with pytest.raises(TokenError) as exc_info:
        await proxy.exchange_authorization_code(cast(Any, _client()), cast(Any, _code()))

    assert exc_info.value.error == "invalid_grant"
    state._code_store.delete.assert_awaited_once_with(key="authorization-code")


async def test_losing_or_retryable_code_exchange_does_not_consume_fastmcp_code() -> None:
    cases: tuple[tuple[Exception, type[BaseException]], ...] = (
        (ExchangeAlreadyClaimedError(), TokenError),
        (OidcPrincipalVerificationUnavailableError(), HTTPException),
    )
    for failure, expected in cases:
        authority = _authority()
        resolver = _principal_resolver()
        proxy = _bare_proxy(authority=authority, resolver=resolver)
        state = cast(Any, proxy)
        state._code_store.get.return_value = SimpleNamespace(
            client_id=CLIENT_ID, idp_tokens={"access_token": "upstream", "token_type": "Bearer"}
        )
        if isinstance(failure, ExchangeAlreadyClaimedError):
            authority.begin_exchange.side_effect = failure
        else:
            resolver.resolve.side_effect = failure

        with pytest.raises(expected):
            await proxy.exchange_authorization_code(cast(Any, _client()), cast(Any, _code()))
        state._code_store.delete.assert_not_awaited()


async def test_refresh_preserves_grant_and_rechecks_same_binding_after_rotation() -> None:
    authority = _authority()
    proxy = _bare_proxy(authority=authority)
    _install_token_payloads(
        proxy,
        {
            "old-refresh": _payload(jti="old-refresh-jti", token_use="refresh"),
            "new-access": _payload(jti="new-access-jti"),
            "new-refresh": _payload(jti="new-refresh-jti", token_use="refresh"),
        },
    )

    async def fastmcp_refresh(
        self: HakuAgentOAuthProxy, client: object, refresh_token: object, scopes: list[str]
    ) -> OAuthToken:
        _ = client, refresh_token
        assert scopes == ["read"]
        assert self._translate_scopes_from_idp(["read"]) == ["read"]
        assert await self._extract_upstream_claims({"access_token": "rotated"}) == {"grant_id": str(GRANT_ID)}
        return _family()

    refresh = RefreshToken(token="old-refresh", client_id=CLIENT_ID, scopes=["read"])
    with patch.object(RetryableRefreshOIDCProxy, "exchange_refresh_token", new=fastmcp_refresh):
        result = await proxy.exchange_refresh_token(cast(Any, _client()), refresh, ["read"])

    assert result.refresh_token == "new-refresh"
    assert authority.resolve_grant.await_count == 2
    assert authority.resolve_grant.await_args_list[0].kwargs == {
        "grant_id": GRANT_ID,
        "client_id": CLIENT_ID,
        "token_scopes": SCOPES,
    }


async def test_refresh_rejects_scope_broadening_inside_fastmcp_hook() -> None:
    proxy = _bare_proxy()
    _install_token_payloads(proxy, {"old-refresh": _payload(jti="old-refresh-jti", token_use="refresh")})

    async def broaden(
        self: HakuAgentOAuthProxy, client: object, refresh_token: object, scopes: list[str]
    ) -> OAuthToken:
        _ = client, refresh_token, scopes
        self._translate_scopes_from_idp(["admin"])
        raise AssertionError("scope hook should reject")

    with (
        patch.object(RetryableRefreshOIDCProxy, "exchange_refresh_token", new=broaden),
        pytest.raises(TokenError) as exc_info,
    ):
        await proxy.exchange_refresh_token(
            cast(Any, _client()), RefreshToken(token="old-refresh", client_id=CLIENT_ID, scopes=["read"]), ["read"]
        )

    assert exc_info.value.error == "invalid_scope"


async def test_revoked_during_refresh_never_returns_rotated_credentials() -> None:
    authority = _authority()
    authority.resolve_grant.side_effect = [_authorization(), GrantRejectedError()]
    proxy = _bare_proxy(authority=authority)
    _install_token_payloads(
        proxy,
        {
            "old-refresh": _payload(jti="old-refresh-jti", token_use="refresh"),
            "new-access": _payload(jti="new-access-jti"),
            "new-refresh": _payload(jti="new-refresh-jti", token_use="refresh"),
        },
    )

    with (
        patch.object(RetryableRefreshOIDCProxy, "exchange_refresh_token", new=AsyncMock(return_value=_family())),
        pytest.raises(TokenError) as exc_info,
    ):
        await proxy.exchange_refresh_token(
            cast(Any, _client()), RefreshToken(token="old-refresh", client_id=CLIENT_ID, scopes=["read"]), ["read"]
        )

    assert exc_info.value.error == "invalid_grant"


async def test_access_load_resolves_grant_before_and_after_fastmcp_validation() -> None:
    authority = _authority()
    proxy = _bare_proxy(authority=authority)
    _install_token_payloads(proxy, {"local-access": _payload(jti="access-jti")})
    seen_contexts: list[object] = []

    async def fastmcp_load(self: object, token: str) -> AccessToken:
        assert token == "local-access"
        seen_contexts.append(current_grant_request_context())
        return AccessToken(
            token="upstream-access",
            client_id="upstream-oidc-client",
            scopes=["read"],
            expires_at=None,
            claims={"upstream_claims": {"grant_id": str(GRANT_ID)}},
        )

    with patch.object(RetryableRefreshOIDCProxy, "load_access_token", new=fastmcp_load):
        access = await proxy.load_access_token("local-access")

    assert access is not None
    assert access.client_id == CLIENT_ID
    assert len(seen_contexts) == 1
    current = seen_contexts[0]
    assert cast(Any, current).authorization.actor.binding_id == BINDING_ID
    assert current_grant_request_context() is None
    assert authority.resolve_grant.await_count == 2


async def test_clean_invalid_or_revoked_bearer_is_a_clean_non_match() -> None:
    proxy = _bare_proxy()
    cast(Any, proxy)._jwt_issuer.verify_token.side_effect = ValueError("not our JWT")
    assert await proxy.load_access_token("random-bearer") is None

    authority = _authority()
    authority.resolve_grant.side_effect = GrantRejectedError()
    proxy = _bare_proxy(authority=authority)
    _install_token_payloads(proxy, {"revoked": _payload(jti="revoked-jti")})
    with patch.object(RetryableRefreshOIDCProxy, "load_access_token", new=AsyncMock()) as parent:
        assert await proxy.load_access_token("revoked") is None
    parent.assert_not_awaited()


async def test_transparent_refresh_transport_failure_survives_fastmcp_blanket_catch() -> None:
    proxy = _bare_proxy()
    _install_token_payloads(proxy, {"local-access": _payload(jti="access-jti")})
    upstream_failure = httpx.ConnectError(
        "temporary DNS failure", request=httpx.Request("POST", "https://auth.example.test/token")
    )

    async def fastmcp_try(self: object, token_set: object) -> object:
        _ = self, token_set
        raise upstream_failure

    async def fastmcp_load(self: HakuAgentOAuthProxy, token: str) -> None:
        _ = token
        try:
            await self._try_transparent_refresh(object())
        except httpx.ConnectError:
            # This is the blanket conversion in OAuthProxy.load_access_token.
            return
        raise AssertionError("refresh was expected to fail")

    with (
        patch.object(RetryableRefreshOIDCProxy, "_try_transparent_refresh", new=fastmcp_try),
        patch.object(RetryableRefreshOIDCProxy, "load_access_token", new=fastmcp_load),
        pytest.raises(BearerVerificationUnavailableError) as exc_info,
    ):
        await proxy.load_access_token("local-access")

    assert exc_info.value.__cause__ is upstream_failure


async def test_storage_wrapper_preserves_failure_through_fastmcp_blanket_catch() -> None:
    proxy = _bare_proxy()
    _install_token_payloads(proxy, {"local-access": _payload(jti="access-jti")})
    storage_failure = RuntimeError("Postgres temporarily unavailable")
    backend = SimpleNamespace(get=AsyncMock(side_effect=storage_failure))
    storage = BearerFailureObservingKeyValue(cast(Any, backend))

    async def swallowed(self: object, token: str) -> None:
        _ = self, token
        try:
            await storage.get("access-jti", collection="oauth")
        except RuntimeError:
            # FastMCP's state model wrappers convert this failure to a miss.
            return
        raise AssertionError("storage access was expected to fail")

    with (
        patch.object(RetryableRefreshOIDCProxy, "load_access_token", new=swallowed),
        pytest.raises(BearerVerificationUnavailableError) as exc_info,
    ):
        await proxy.load_access_token("local-access")

    assert exc_info.value.__cause__ is storage_failure
    backend.get.assert_awaited_once_with(collection="oauth", key="access-jti")

    # The observation belongs only to that bearer request.
    with patch.object(RetryableRefreshOIDCProxy, "load_access_token", new=AsyncMock(return_value=None)):
        assert await proxy.load_access_token("local-access") is None


async def test_jwks_verifier_preserves_fetch_failure_through_fastmcp_non_match() -> None:
    proxy = _bare_proxy()
    _install_token_payloads(proxy, {"local-access": _payload(jti="access-jti")})
    jwks_failure = httpx.ConnectError(
        "JWKS endpoint unavailable", request=httpx.Request("GET", "https://auth.example.test/jwks/")
    )
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = jwks_failure
    verifier = FailureObservingJWTVerifier(
        jwks_uri="https://auth.example.test/jwks/",
        issuer="https://auth.example.test/",
        audience=CLIENT_ID,
        algorithm="RS256",
        http_client=cast(Any, http_client),
    )

    async def swallowed(self: object, token: str) -> None:
        _ = self, token
        # A valid header reaches JWKS lookup; JWTVerifier then deliberately
        # converts its operational ValueError into a clean verifier non-match.
        assert await verifier.verify_token("eyJhbGciOiJSUzI1NiIsImtpZCI6ImtleSJ9.e30.signature") is None

    with (
        patch.object(RetryableRefreshOIDCProxy, "load_access_token", new=swallowed),
        pytest.raises(BearerVerificationUnavailableError) as exc_info,
    ):
        await proxy.load_access_token("local-access")

    assert exc_info.value.__cause__ is jwks_failure


async def test_bearer_request_contexts_reset_when_fastmcp_raises() -> None:
    proxy = _bare_proxy()
    _install_token_payloads(proxy, {"local-access": _payload(jti="access-jti")})

    async def fail_during_verification(self: object, token: str) -> None:
        _ = self, token
        assert current_grant_request_context() is not None
        raise RuntimeError("FastMCP verification failed")

    with (
        patch.object(RetryableRefreshOIDCProxy, "load_access_token", new=fail_during_verification),
        pytest.raises(RuntimeError, match="verification failed"),
    ):
        await proxy.load_access_token("local-access")

    assert current_grant_request_context() is None
    observe_bearer_operational_failure(RuntimeError("outside any bearer request"))
    with patch.object(RetryableRefreshOIDCProxy, "load_access_token", new=AsyncMock(return_value=None)):
        assert await proxy.load_access_token("local-access") is None


async def test_successful_fastmcp_recovery_wins_over_observed_transient_attempt() -> None:
    proxy = _bare_proxy()
    _install_token_payloads(proxy, {"local-access": _payload(jti="access-jti")})

    async def recovered(self: object, token: str) -> AccessToken:
        _ = self, token
        observe_bearer_operational_failure(RuntimeError("stale worker refresh lost"))
        return AccessToken(
            token="upstream-access",
            client_id="upstream-client",
            scopes=["read"],
            expires_at=None,
            claims={"upstream_claims": {"grant_id": str(GRANT_ID)}},
        )

    with patch.object(RetryableRefreshOIDCProxy, "load_access_token", new=recovered):
        assert await proxy.load_access_token("local-access") is not None


async def test_failure_preserving_multi_auth_still_allows_later_static_match() -> None:
    outage = BearerVerificationUnavailableError("OAuth state is unavailable")
    oauth = _TestVerifier(failure=outage)
    static_token = _verified_access_token(claims={"credential_kind": "static"})
    static = _TestVerifier(result=static_token)
    auth = HakuFailurePreservingMultiAuth(server=oauth, verifiers=[static])

    assert await auth.verify_token("credential") is static_token
    assert oauth.calls == ["credential"]
    assert static.calls == ["credential"]


async def test_failure_preserving_multi_auth_rethrows_only_classified_outage_after_fallthrough() -> None:
    outage = BearerVerificationUnavailableError("OAuth state is unavailable")
    auth = HakuFailurePreservingMultiAuth(
        server=_TestVerifier(failure=outage),
        verifiers=[_TestVerifier(failure=RuntimeError("unrelated verifier failure")), _TestVerifier()],
    )

    with pytest.raises(BearerVerificationUnavailableError) as exc_info:
        await auth.verify_token("credential")
    assert exc_info.value is outage
    assert exc_info.value.status_code == 503

    auth_without_marker = HakuFailurePreservingMultiAuth(
        server=_TestVerifier(failure=RuntimeError("ordinary failure")), verifiers=[_TestVerifier()]
    )
    assert await auth_without_marker.verify_token("credential") is None


def test_failure_preserving_multi_auth_delegates_routes_and_renders_retryable_response() -> None:
    server = _TestVerifier()
    expected_routes = [cast(Any, object())]
    server.get_routes = Mock(return_value=expected_routes)  # type: ignore[method-assign]
    auth = HakuFailurePreservingMultiAuth(server=server, verifiers=[])

    assert auth.get_routes("/mcp") is expected_routes
    server.get_routes.assert_called_once_with("/mcp")
    assert HakuFailurePreservingMultiAuth.get_routes is MultiAuth.get_routes

    authentication = auth.get_middleware()[0]
    on_error = cast(Any, authentication.kwargs["on_error"])
    response = on_error(cast(Any, object()), BearerVerificationUnavailableError("retry later"))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"


async def test_revoke_marks_haku_grant_before_fastmcp_best_effort_cleanup() -> None:
    authority = _authority()
    events: list[str] = []

    async def revoke_grant(**kwargs: object) -> None:
        assert kwargs == {"grant_id": GRANT_ID}
        events.append("haku-revoke")

    authority.revoke_grant.side_effect = revoke_grant
    proxy = _bare_proxy(authority=authority)
    _install_token_payloads(proxy, {"local-access": _payload(jti="access-jti")})

    async def fastmcp_revoke(self: object, token: object) -> None:
        _ = self, token
        events.append("fastmcp-cleanup")

    with patch.object(RetryableRefreshOIDCProxy, "revoke_token", new=fastmcp_revoke):
        await proxy.revoke_token(
            AccessToken(token="local-access", client_id=CLIENT_ID, scopes=["read"], expires_at=None)
        )

    assert events == ["haku-revoke", "fastmcp-cleanup"]


def _verified_access_token(*, claims: dict[str, Any] | None = None) -> AccessToken:
    return AccessToken(
        token="upstream-access",
        client_id=CLIENT_ID,
        scopes=["read"],
        expires_at=None,
        claims=claims if claims is not None else {"upstream_claims": {"grant_id": str(GRANT_ID)}},
    )


async def test_first_tool_call_activates_once_and_later_calls_are_idempotent() -> None:
    authority = _authority()
    middleware = HakuAgentGrantMiddleware(cast(Any, authority))
    actor = _authorization().actor
    dispatched: list[str] = []

    async def dispatch(context: object) -> Any:
        _ = context
        assert get_agent_actor() == actor
        dispatched.append("dispatch")
        return cast(Any, object())

    with patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=_verified_access_token()):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))

    assert authority.activate_for_tool_call.await_count == 2
    assert authority.activate_for_tool_call.await_args_list[0].kwargs == {
        "grant_id": GRANT_ID,
        "client_id": CLIENT_ID,
        "token_scopes": SCOPES,
    }
    assert dispatched == ["dispatch", "dispatch"]
    with pytest.raises(ToolError, match="outside"):
        get_agent_actor()


async def test_agent_actor_context_resets_when_tool_dispatch_raises() -> None:
    middleware = HakuAgentGrantMiddleware(cast(Any, _authority()))

    async def fail(context: object) -> Any:
        _ = context
        assert get_agent_actor() == _authorization().actor
        raise RuntimeError("tool dispatch failed")

    with (
        patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=_verified_access_token()),
        pytest.raises(RuntimeError, match="dispatch failed"),
    ):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, fail))

    with pytest.raises(ToolError, match="outside"):
        get_agent_actor()


async def test_rejected_tool_call_grant_never_dispatches() -> None:
    authority = _authority()
    authority.activate_for_tool_call.side_effect = GrantRejectedError()
    middleware = HakuAgentGrantMiddleware(cast(Any, authority))
    dispatch = AsyncMock()

    with (
        patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=_verified_access_token()),
        pytest.raises(ToolError, match="not active"),
    ):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))

    dispatch.assert_not_awaited()


async def test_tool_call_authority_outage_is_typed_and_retryable_without_dispatch() -> None:
    authority = _authority()
    authority.activate_for_tool_call.side_effect = AgentGrantAuthorityUnavailableError()
    middleware = HakuAgentGrantMiddleware(cast(Any, authority))
    dispatch = AsyncMock()

    with (
        patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=_verified_access_token()),
        pytest.raises(AgentActorResolutionUnavailableError, match="retry"),
    ):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))

    dispatch.assert_not_awaited()


@pytest.mark.parametrize(
    "authorization",
    [
        replace(_authorization(), actor=replace(_authorization().actor, binding_id=UUID(int=0))),
        replace(_authorization(), actor=replace(_authorization().actor, agent_id=UUID(int=0))),
        replace(_authorization(), actor=replace(_authorization().actor, operator_id=UUID(int=0))),
        replace(_authorization(), client_id="different-client"),
        replace(_authorization(), allowed_scopes=frozenset()),
    ],
)
async def test_tool_call_rejects_inconsistent_authority_resolution_before_dispatch(
    authorization: GrantAuthorization,
) -> None:
    authority = _authority()
    authority.activate_for_tool_call.return_value = authorization
    middleware = HakuAgentGrantMiddleware(cast(Any, authority))
    dispatch = AsyncMock()

    with (
        patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=_verified_access_token()),
        pytest.raises(ToolError, match="not active"),
    ):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))

    dispatch.assert_not_awaited()


async def test_static_credential_resolves_canonical_actor_for_dependency() -> None:
    authority = _authority()
    actor = _authorization().actor
    static_actor_resolver = SimpleNamespace(resolve_static_actor=AsyncMock(return_value=actor))
    middleware = HakuAgentGrantMiddleware(
        cast(Any, authority), static_actor_resolver=cast(StaticAgentActorResolver, static_actor_resolver)
    )
    token = _verified_access_token(claims={"credential_kind": "static"})

    async def dispatch(context: object) -> Any:
        _ = context
        assert get_agent_actor() == actor
        return cast(Any, object())

    with patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=token):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))

    static_actor_resolver.resolve_static_actor.assert_awaited_once_with(token)
    authority.activate_for_tool_call.assert_not_awaited()
    with pytest.raises(ToolError, match="outside"):
        get_agent_actor()


async def test_unrecognized_static_tool_credential_fails_before_dispatch() -> None:
    authority = _authority()
    static_actor_resolver = SimpleNamespace(resolve_static_actor=AsyncMock(return_value=None))
    middleware = HakuAgentGrantMiddleware(
        cast(Any, authority), static_actor_resolver=cast(StaticAgentActorResolver, static_actor_resolver)
    )
    dispatch = AsyncMock()
    token = _verified_access_token(claims={"credential_kind": "unknown"})

    with (
        patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=token),
        pytest.raises(ToolError, match="missing"),
    ):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))

    static_actor_resolver.resolve_static_actor.assert_awaited_once_with(token)
    authority.activate_for_tool_call.assert_not_awaited()
    dispatch.assert_not_awaited()


async def test_static_actor_authority_outage_is_typed_and_retryable() -> None:
    authority = _authority()
    static_actor_resolver = SimpleNamespace(
        resolve_static_actor=AsyncMock(side_effect=AgentActorAuthorityUnavailableError())
    )
    middleware = HakuAgentGrantMiddleware(
        cast(Any, authority), static_actor_resolver=cast(StaticAgentActorResolver, static_actor_resolver)
    )
    dispatch = AsyncMock()
    token = _verified_access_token(claims={"credential_kind": "static"})

    with (
        patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=token),
        pytest.raises(AgentActorResolutionUnavailableError, match="retry"),
    ):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))

    dispatch.assert_not_awaited()


async def test_static_resolver_cannot_bypass_malformed_oauth_grant() -> None:
    authority = _authority()
    static_actor_resolver = SimpleNamespace(resolve_static_actor=AsyncMock(return_value=_authorization().actor))
    middleware = HakuAgentGrantMiddleware(
        cast(Any, authority), static_actor_resolver=cast(StaticAgentActorResolver, static_actor_resolver)
    )
    dispatch = AsyncMock()
    malformed = _verified_access_token(claims={"upstream_claims": {"grant_id": "not-a-uuid"}})

    with (
        patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=malformed),
        pytest.raises(ToolError, match="invalid"),
    ):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))

    static_actor_resolver.resolve_static_actor.assert_not_awaited()
    authority.activate_for_tool_call.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.parametrize(
    "token",
    [
        None,
        _verified_access_token(claims={}),
        _verified_access_token(claims={"upstream_claims": {"grant_id": "not-a-uuid"}}),
        _verified_access_token(claims={"upstream_claims": {"grant_id": str(GRANT_ID), "agent_id": "forbidden-copy"}}),
    ],
)
async def test_missing_or_malformed_tool_call_claims_fail_before_authority_and_dispatch(
    token: AccessToken | None,
) -> None:
    authority = _authority()
    middleware = HakuAgentGrantMiddleware(cast(Any, authority))
    dispatch = AsyncMock()

    with (
        patch("haku.console.mcp_auth.fastmcp_adapter.get_access_token", return_value=token),
        pytest.raises(ToolError, match=r"missing|invalid"),
    ):
        await middleware.on_call_tool(cast(Any, SimpleNamespace()), cast(Any, dispatch))

    authority.activate_for_tool_call.assert_not_awaited()
    dispatch.assert_not_awaited()


def test_fastmcp_344_surface_and_adapter_containment_are_pinned() -> None:
    assert_fastmcp_adapter_compatibility()
    assert issubclass(HakuAgentOAuthProxy, RetryableRefreshOIDCProxy)
    assert inspect.signature(HakuAgentOAuthProxy.get_token_verifier, eval_str=True) == inspect.signature(
        RetryableRefreshOIDCProxy.get_token_verifier, eval_str=True
    )
    source = inspect.getsource(HakuAgentOAuthProxy)
    assert source.count("self._code_store.") == 2
    for forbidden in (
        "_transaction_store",
        "def get_routes",
        "def _handle_idp_callback",
        "def load_authorization_code",
        "def register_client",
    ):
        assert forbidden not in source
    assert HakuAgentOAuthProxy.get_routes is OAuthProxy.get_routes
    assert HakuAgentOAuthProxy._handle_idp_callback is OAuthProxy._handle_idp_callback
    assert HakuFailurePreservingMultiAuth.get_routes is MultiAuth.get_routes
    assert HakuFailurePreservingMultiAuth.get_well_known_routes is MultiAuth.get_well_known_routes

    exchange_source = inspect.getsource(OAuthProxy.exchange_authorization_code)
    refresh_source = inspect.getsource(OAuthProxy.exchange_refresh_token)
    load_source = inspect.getsource(OAuthProxy.load_access_token)
    assert "self._extract_upstream_claims(idp_tokens)" in exchange_source
    assert "self._translate_scopes_from_idp(granted_scopes)" in exchange_source
    assert "self._extract_upstream_claims(" in refresh_source
    assert "self._translate_scopes_from_idp(refreshed_scopes)" in refresh_source
    assert "await self._try_transparent_refresh(" in load_source
    assert "except Exception as e:" in load_source

    multi_auth_source = inspect.getsource(MultiAuth.verify_token)
    assert "for source in self._sources:" in multi_auth_source
    assert "except Exception:" in multi_auth_source


if __name__ == "__main__":
    pytest_bazel.main()

"""Compatibility contract for FastMCP OAuth behavior Ducktape adapts."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_bazel
from fastmcp.server.auth.auth import AccessToken, AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from mcp.server.auth.provider import RefreshToken, TokenError


def _bare_proxy() -> OAuthProxy:
    return object.__new__(OAuthProxy)


async def test_authorization_code_exchange_reads_raw_tokens_before_deleting_code() -> None:
    proxy = _bare_proxy()
    state = cast(Any, proxy)
    idp_tokens = {"access_token": "upstream-access", "scope": "openid profile", "token_type": "Bearer"}
    events: list[str] = []
    state._code_store = AsyncMock()

    async def read_code(*, key: str) -> SimpleNamespace:
        assert key == "stored-code"
        events.append("read-code")
        return SimpleNamespace(idp_tokens=idp_tokens)

    state._code_store.get.side_effect = read_code

    async def delete_code(*, key: str) -> None:
        assert key == "stored-code"
        events.append("delete-code")

    state._code_store.delete.side_effect = delete_code
    state._upstream_token_store = AsyncMock()

    async def store_upstream_tokens(*, key: str, value: Any, ttl: int) -> None:
        assert key
        assert ttl == 3600
        assert value.client_id == "dcr-client"
        assert value.raw_token_data == idp_tokens
        events.append("store-upstream-tokens")

    state._upstream_token_store.put.side_effect = store_upstream_tokens
    state._jti_mapping_store = AsyncMock()
    state._refresh_token_store = AsyncMock()
    state._fallback_access_token_expiry_seconds = 3600
    state._fastmcp_access_token_expiry_seconds = None
    state._jwt_issuer = Mock()
    state._jwt_issuer.issue_access_token.return_value = "fastmcp-access"
    state._extract_upstream_claims = AsyncMock(return_value=None)

    token = await OAuthProxy.exchange_authorization_code(
        proxy,
        cast(Any, SimpleNamespace(client_id="dcr-client")),
        cast(Any, SimpleNamespace(code="stored-code", scopes=["openid", "profile"])),
    )

    assert token.access_token == "fastmcp-access"
    assert events == ["read-code", "delete-code", "store-upstream-tokens"]
    state._extract_upstream_claims.assert_awaited_once_with(idp_tokens)


def _token_swap_proxy() -> OAuthProxy:
    proxy = _bare_proxy()
    state = cast(Any, proxy)
    state._jwt_issuer = Mock()
    state._jwt_issuer.verify_token.return_value = {"jti": "access-jti"}
    state._jti_mapping_store = AsyncMock()
    state._jti_mapping_store.get.return_value = SimpleNamespace(upstream_token_id="upstream-token-id")
    state._upstream_token_store = AsyncMock()
    state._upstream_token_store.get.return_value = SimpleNamespace(
        upstream_token_id="upstream-token-id",
        client_id="dcr-client",
        access_token="upstream-access",
        refresh_token=None,
        expires_at=time.time() + 3600,
        scope="openid profile",
    )
    state._token_validator = AsyncMock()
    state._refresh_locks = {}
    return proxy


async def test_token_swap_returns_upstream_identity_instead_of_dcr_client_id() -> None:
    proxy = _token_swap_proxy()
    state = cast(Any, proxy)
    state._token_validator.verify_token.return_value = AccessToken(
        token="upstream-access", client_id="upstream-client", scopes=["openid", "profile"], expires_at=None
    )

    validated = await OAuthProxy.load_access_token(proxy, "fastmcp-reference")

    assert validated is not None
    assert validated.client_id == "upstream-client"
    assert validated.client_id != "dcr-client"


async def test_token_swap_erases_oauth_storage_failures() -> None:
    proxy = _token_swap_proxy()
    state = cast(Any, proxy)
    state._jti_mapping_store.get.side_effect = RuntimeError("OAuth storage unavailable")

    assert await OAuthProxy.load_access_token(proxy, "fastmcp-reference") is None


class _RaisingProvider(AuthProvider):
    async def verify_token(self, token: str) -> AccessToken | None:
        raise RuntimeError(f"storage failure while verifying {token}")


class _AcceptingProvider(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        return AccessToken(token=token, client_id="fallback", scopes=[], expires_at=None)


async def test_multi_auth_treats_verifier_exceptions_as_non_matches() -> None:
    auth = MultiAuth(server=_RaisingProvider(), verifiers=[_AcceptingProvider()])

    validated = await auth.verify_token("bearer")

    assert validated is not None
    assert validated.client_id == "fallback"


async def test_external_consent_bypasses_fastmcp_consent_page() -> None:
    proxy = _bare_proxy()
    state = cast(Any, proxy)
    state._resource_url = None
    state._forward_pkce = False
    state._transaction_store = AsyncMock()
    state._require_authorization_consent = "external"
    state._build_upstream_authorize_url = Mock(return_value="https://idp.example.test/authorize")

    result = await OAuthProxy.authorize(
        proxy,
        cast(Any, SimpleNamespace(client_id="dcr-client")),
        cast(
            Any,
            SimpleNamespace(
                resource=None,
                code_challenge=None,
                redirect_uri="https://client.example.test/callback",
                state="client-state",
                scopes=["openid"],
            ),
        ),
    )

    assert result == "https://idp.example.test/authorize"
    state._transaction_store.put.assert_awaited_once()
    state._build_upstream_authorize_url.assert_called_once()


async def test_fastmcp_refresh_preserves_upstream_transport_error_as_cause() -> None:
    proxy = _bare_proxy()
    state = cast(Any, proxy)
    upstream_url = "https://auth.example.test/application/o/token/"
    upstream_failure = httpx.ConnectError("temporary DNS failure", request=httpx.Request("POST", upstream_url))
    state._jwt_issuer = Mock()
    state._jwt_issuer.verify_token.return_value = {"jti": "refresh-jti"}
    state._jti_mapping_store = AsyncMock()
    state._jti_mapping_store.get.return_value = SimpleNamespace(upstream_token_id="upstream-token-id")
    state._upstream_token_store = AsyncMock()
    state._upstream_token_store.get.return_value = SimpleNamespace(refresh_token="authentik-refresh-token")
    state._upstream_token_endpoint = upstream_url
    state._extra_token_params = {}
    upstream_client = Mock()
    upstream_client.refresh_token = AsyncMock(side_effect=upstream_failure)
    upstream_client.aclose = AsyncMock()
    state._create_upstream_oauth_client = Mock(return_value=upstream_client)

    with pytest.raises(TokenError) as exc_info:
        await OAuthProxy.exchange_refresh_token(
            proxy,
            cast(Any, SimpleNamespace(client_id="dcr-client")),
            RefreshToken(token="fastmcp-refresh", client_id="dcr-client", scopes=["openid"]),
            ["openid"],
        )

    assert exc_info.value.error == "invalid_grant"
    assert exc_info.value.__cause__ is upstream_failure


async def test_public_access_token_revocation_does_not_delete_token_family() -> None:
    proxy = _bare_proxy()
    state = cast(Any, proxy)
    state._upstream_revocation_endpoint = None
    state._refresh_token_store = AsyncMock()
    state._jti_mapping_store = AsyncMock()
    state._upstream_token_store = AsyncMock()

    await OAuthProxy.revoke_token(
        proxy, AccessToken(token="fastmcp-access", client_id="dcr-client", scopes=[], expires_at=None)
    )

    state._refresh_token_store.delete.assert_not_awaited()
    state._jti_mapping_store.delete.assert_not_awaited()
    state._upstream_token_store.delete.assert_not_awaited()


async def test_public_refresh_revocation_deletes_only_refresh_metadata() -> None:
    proxy = _bare_proxy()
    state = cast(Any, proxy)
    state._upstream_revocation_endpoint = None
    state._refresh_token_store = AsyncMock()
    state._jti_mapping_store = AsyncMock()
    state._upstream_token_store = AsyncMock()

    await OAuthProxy.revoke_token(proxy, RefreshToken(token="fastmcp-refresh", client_id="dcr-client", scopes=[]))

    state._refresh_token_store.delete.assert_awaited_once()
    state._jti_mapping_store.delete.assert_not_awaited()
    state._upstream_token_store.delete.assert_not_awaited()


if __name__ == "__main__":
    pytest_bazel.main()

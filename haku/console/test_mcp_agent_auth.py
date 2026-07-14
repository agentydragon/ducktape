"""Tests for Haku's Agent-facing MCP authentication composition."""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock, patch

import jwt
import pytest
import pytest_bazel
from pydantic import SecretStr

from haku.console.config import McpOAuthConfig, OperatorOidcConfig, Settings
from haku.console.mcp_agent_auth import OAuthMcpAuth, StaticMcpAuth, build_auth, operator_subject_from_idp_tokens
from haku.console.mcp_config import ResolvedStaticAgent
from haku.console.mcp_operator_oauth import PostgresMcpOperatorOAuthStore
from mcp_infra.persistence import PostgresPersistence


def _id_token(claims: dict[str, object]) -> str:
    # The current resolver decodes without signature verification; P1 replaces this contract.
    return jwt.encode(claims, "unused-signing-key", algorithm="HS256")


def _settings(*, mcp_oauth: McpOAuthConfig | None = None) -> Settings:
    return Settings(
        haku_ui_url="https://haku-ui.test",
        public_base_url="https://haku.test",
        database_url=SecretStr("postgresql+psycopg://db.test/haku"),
        operator_oidc=OperatorOidcConfig(
            issuer="https://auth.test/application/o/haku-console/",
            client_id="console",
            client_secret=SecretStr("secret"),
            session_secret=SecretStr("session-secret"),
        ),
        mcp_oauth=mcp_oauth,
    )


def _store() -> PostgresMcpOperatorOAuthStore:
    return cast(PostgresMcpOperatorOAuthStore, Mock(spec=PostgresMcpOperatorOAuthStore))


def _static_agent() -> ResolvedStaticAgent:
    return ResolvedStaticAgent(agent="haku", token=SecretStr("agent-token"), operator_subject="operator-42")


def test_extracts_sub_not_username() -> None:
    idp = {"id_token": _id_token({"sub": "42", "preferred_username": "agentydragon", "email": "a@b.c"})}
    assert operator_subject_from_idp_tokens(idp) == "42"


def test_none_without_id_token_or_sub() -> None:
    assert operator_subject_from_idp_tokens({"access_token": "opaque"}) is None
    # A username but no `sub` is not enough — the link keys on the opaque subject only.
    assert operator_subject_from_idp_tokens({"id_token": _id_token({"preferred_username": "agentydragon"})}) is None


async def test_static_only_auth_maps_bearer_to_namespaced_agent_identity() -> None:
    auth = build_auth(_settings(), [_static_agent()], operator_oauth_store=_store())

    assert isinstance(auth, StaticMcpAuth)
    access = await auth.provider.verify_token("agent-token")
    assert access is not None
    assert access.client_id == "static-agent:haku"


def test_build_auth_rejects_missing_credentials() -> None:
    with pytest.raises(ValueError, match="no configured credential"):
        build_auth(_settings(), [], operator_oauth_store=_store())


async def test_oauth_auth_composes_storage_static_bearer_and_operator_link() -> None:
    settings = _settings(
        mcp_oauth=McpOAuthConfig(
            oidc_issuer="https://auth.test/application/o/haku-agent/",
            oidc_client_id="haku-agent",
            oidc_client_secret=SecretStr("oauth-secret"),
            persistence=PostgresPersistence(kind="postgres", url="postgresql://db.test/haku"),
        )
    )
    store = _store()
    storage = Mock()
    provider = Mock()
    with (
        patch("haku.console.mcp_agent_auth.build_shared_client_storage", return_value=storage),
        patch("haku.console.mcp_agent_auth.build_authentik_auth", return_value=provider) as build_provider,
    ):
        auth = build_auth(settings, [_static_agent()], operator_oauth_store=store)

    assert isinstance(auth, OAuthMcpAuth)
    assert auth.provider is provider
    assert auth.storage is storage
    config = build_provider.call_args.args[0]
    assert config.oidc_issuer == "https://auth.test/application/o/haku-agent/"
    assert config.public_base_url == "https://haku.test/mcp"
    kwargs = build_provider.call_args.kwargs
    assert kwargs["client_storage"] is storage
    assert len(kwargs["extra_verifiers"]) == 1

    callback = kwargs["on_client_authorized"]
    await callback("dcr-claude", {"id_token": _id_token({"sub": "operator-42"})})
    cast(Mock, store.bind_agent_operator).assert_called_once_with(
        agent_dcr_client_id="dcr-claude", operator_subject="operator-42"
    )
    with pytest.raises(RuntimeError, match="carried no `sub` claim"):
        await callback("dcr-invalid", {"access_token": "opaque"})


if __name__ == "__main__":
    pytest_bazel.main()

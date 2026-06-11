"""Tests for the URL helpers on ServerSettings / AuthentikAuthConfig.

The only non-trivial piece is `authentik_token_endpoint()` — it needs to
derive the global Authentik token endpoint from a per-provider issuer URL
while preserving reverse-proxy path prefixes and rejecting non-Authentik
issuer shapes. Pin the behaviour.
"""

from __future__ import annotations

import pytest
import pytest_bazel

from x.authentik_mcp_poc.config import ServerSettings


def _settings(issuer: str) -> ServerSettings:
    return ServerSettings(
        oidc_issuer=issuer,
        oidc_client_id="id",
        oidc_client_secret="secret",
        public_base_url="https://example.com",
        backend_url="https://backend.example.com",
        backend_oidc_client_id="backend-id",
    )


def test_token_endpoint_simple() -> None:
    cfg = _settings("https://auth.allegedly.works/application/o/authentik-mcp-poc/").auth_config()
    assert cfg.authentik_token_endpoint() == "https://auth.allegedly.works/application/o/token/"


def test_token_endpoint_preserves_reverse_proxy_prefix() -> None:
    cfg = _settings("https://example.com/auth/application/o/mcp/").auth_config()
    assert cfg.authentik_token_endpoint() == "https://example.com/auth/application/o/token/"


def test_token_endpoint_accepts_unterminated_issuer() -> None:
    cfg = _settings("https://auth.allegedly.works/application/o/mcp").auth_config()
    assert cfg.authentik_token_endpoint() == "https://auth.allegedly.works/application/o/token/"


def test_token_endpoint_rejects_non_authentik_issuer() -> None:
    cfg = _settings("https://keycloak.example.com/realms/mcp").auth_config()
    with pytest.raises(ValueError, match="Authentik per-provider issuer path"):
        cfg.authentik_token_endpoint()


def test_token_endpoint_rejects_missing_slug() -> None:
    cfg = _settings("https://auth.allegedly.works/application/o/").auth_config()
    with pytest.raises(ValueError, match="Authentik per-provider issuer path"):
        cfg.authentik_token_endpoint()


if __name__ == "__main__":
    pytest_bazel.main()

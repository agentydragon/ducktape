"""Tests for the URL helpers on ServerSettings.

The only non-trivial piece in `config.py` is
`authentik_token_endpoint()` — it needs to derive the global Authentik
token endpoint from a per-provider issuer URL while preserving reverse-proxy
path prefixes and rejecting non-Authentik issuer shapes. Pin the behaviour.
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
    s = _settings("https://auth.allegedly.works/application/o/authentik-mcp-poc/")
    assert s.authentik_token_endpoint() == "https://auth.allegedly.works/application/o/token/"


def test_token_endpoint_preserves_reverse_proxy_prefix() -> None:
    # Authentik mounted under /auth/ behind a reverse proxy — the prefix
    # must survive the transformation.
    s = _settings("https://example.com/auth/application/o/mcp/")
    assert s.authentik_token_endpoint() == "https://example.com/auth/application/o/token/"


def test_token_endpoint_accepts_unterminated_issuer() -> None:
    s = _settings("https://auth.allegedly.works/application/o/mcp")  # no trailing slash
    assert s.authentik_token_endpoint() == "https://auth.allegedly.works/application/o/token/"


def test_token_endpoint_rejects_non_authentik_issuer() -> None:
    s = _settings("https://keycloak.example.com/realms/mcp")
    with pytest.raises(ValueError, match="Authentik per-provider issuer path"):
        s.authentik_token_endpoint()


def test_token_endpoint_rejects_missing_slug() -> None:
    s = _settings("https://auth.allegedly.works/application/o/")
    with pytest.raises(ValueError, match="Authentik per-provider issuer path"):
        s.authentik_token_endpoint()


if __name__ == "__main__":
    pytest_bazel.main()

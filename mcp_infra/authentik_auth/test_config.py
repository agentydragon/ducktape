"""Tests for Authentik authentication configuration."""

import pytest
import pytest_bazel

from mcp_infra.authentik_auth.config import AuthentikAuthConfig, DirectJwtTrust


def _config(
    issuer: str = "https://auth.example.com/application/o/test/",
    public_base_url: str = "https://mcp.example.com",
    proxy_client_id: str | None = None,
) -> AuthentikAuthConfig:
    return AuthentikAuthConfig(
        oidc_issuer=issuer,
        oidc_client_id="id",
        oidc_client_secret="secret",
        public_base_url=public_base_url,
        proxy_client_id=proxy_client_id,
    )


def test_token_endpoint_simple() -> None:
    assert _config().authentik_token_endpoint() == "https://auth.example.com/application/o/token/"


def test_token_endpoint_preserves_reverse_proxy_prefix() -> None:
    cfg = _config("https://example.com/auth/application/o/test/")
    assert cfg.authentik_token_endpoint() == "https://example.com/auth/application/o/token/"


def test_token_endpoint_accepts_unterminated_issuer() -> None:
    cfg = _config("https://auth.example.com/application/o/test")
    assert cfg.authentik_token_endpoint() == "https://auth.example.com/application/o/token/"


def test_token_endpoint_rejects_non_authentik_issuer() -> None:
    with pytest.raises(ValueError, match="Authentik per-provider issuer path"):
        _config("https://keycloak.example.com/realms/test").authentik_token_endpoint()


def test_token_endpoint_rejects_missing_slug() -> None:
    with pytest.raises(ValueError, match="Authentik per-provider issuer path"):
        _config("https://auth.example.com/application/o/").authentik_token_endpoint()


def test_normalized_public_base_url_strips_trailing_slash() -> None:
    cfg = _config(public_base_url="https://mcp.example.com/")
    assert cfg.normalized_public_base_url() == "https://mcp.example.com"


def test_direct_jwt_trust_requires_an_audience() -> None:
    with pytest.raises(ValueError, match="at least 1 item"):
        DirectJwtTrust(issuer="https://auth.example.com/application/o/machine/", audiences=())


if __name__ == "__main__":
    pytest_bazel.main()

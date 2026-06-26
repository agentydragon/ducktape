"""Tests for ServerSettings + AuthentikAuthConfig wiring.

URL-derivation tests live with `AuthentikAuthConfig` itself in
<../../mcp_infra/authentik_auth/test_auth.py>; here we just pin that the
nested Pydantic model loads/omits correctly on `ServerSettings`.
"""

from __future__ import annotations

import pytest
import pytest_bazel

from grocy_mcp.mcp_types import ServerSettings
from mcp_infra.authentik_auth.auth import AuthentikAuthConfig


def test_auth_none_when_unset() -> None:
    assert ServerSettings(grocy_url="https://grocy.example.com").auth is None


def test_extra_jwt_issuers_parses_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment passes GROCY_MCP_AUTH__EXTRA_JWT_ISSUERS as a JSON list; pin
    that pydantic-settings parses it into the nested tuple field (else the server
    would crash on boot)."""
    for key, value in {
        "GROCY_MCP_GROCY_URL": "https://grocy.example.com",
        "GROCY_MCP_AUTH__OIDC_ISSUER": "https://auth.example.com/application/o/grocy-mcp/",
        "GROCY_MCP_AUTH__OIDC_CLIENT_ID": "id",
        "GROCY_MCP_AUTH__OIDC_CLIENT_SECRET": "secret",
        "GROCY_MCP_AUTH__PUBLIC_BASE_URL": "https://grocy-mcp.example.com",
        "GROCY_MCP_AUTH__EXTRA_JWT_ISSUERS": '["https://auth.example.com/application/o/machine/"]',
    }.items():
        monkeypatch.setenv(key, value)

    settings = ServerSettings()
    assert settings.auth is not None
    assert settings.auth.extra_jwt_issuers == ("https://auth.example.com/application/o/machine/",)


def test_auth_round_trips_through_settings() -> None:
    settings = ServerSettings(
        grocy_url="https://grocy.example.com",
        auth=AuthentikAuthConfig(
            oidc_issuer="https://auth.example.com/application/o/grocy-mcp/",
            oidc_client_id="id",
            oidc_client_secret="secret",
            public_base_url="https://grocy-mcp.example.com",
            proxy_client_id="grocy-proxy-id",
        ),
    )
    assert settings.auth is not None
    assert settings.auth.authentik_token_endpoint() == "https://auth.example.com/application/o/token/"


if __name__ == "__main__":
    pytest_bazel.main()

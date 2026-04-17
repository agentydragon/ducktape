"""Tests for ServerSettings + AuthentikAuthConfig wiring.

URL-derivation tests live with `AuthentikAuthConfig` itself in
<../../mcp_infra/authentik_auth/test_auth.py>; here we just pin that the
nested Pydantic model loads/omits correctly on `ServerSettings`.
"""

from __future__ import annotations

import pytest_bazel

from mcp_infra.authentik_auth.auth import AuthentikAuthConfig
from x.grocy_mcp.config import ServerSettings


def test_auth_none_when_unset() -> None:
    assert ServerSettings(grocy_url="https://grocy.example.com").auth is None


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

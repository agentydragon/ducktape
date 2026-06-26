"""Tests for ServerSettings + AuthentikAuthConfig wiring.

URL-derivation tests live with `AuthentikAuthConfig` itself in
<../../mcp_infra/authentik_auth/test_auth.py>; here we just pin that the
nested Pydantic model loads/omits correctly on `ServerSettings`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import pytest_bazel

from grocy_mcp.mcp_types import ServerSettings
from mcp_infra.authentik_auth.auth import AuthentikAuthConfig
from mcp_infra.persistence import ValkeyPersistence


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


def test_yaml_config_deep_merges_with_env_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-secret config from GROCY_MCP_CONFIG_FILE (YAML) deep-merges with env, so a
    single `auth` model draws its issuer/URLs/extra_jwt_issuers from the file and its
    secret (`oidc_client_secret`) from env — the split the k8s deployment relies on."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent("""
        grocy_url: https://grocy-sf.example.com
        auth:
          oidc_issuer: https://auth.example.com/application/o/grocy-mcp-sf/
          public_base_url: https://grocy-mcp-sf.example.com
          extra_jwt_issuers:
            - https://auth.example.com/application/o/grocy-mcp-haku-sf/
        persistence:
          kind: valkey
          host: valkey.example.com
          db: 0
        """)
    )
    monkeypatch.setenv("GROCY_MCP_CONFIG_FILE", str(config_file))
    # Secrets stay in env (a k8s Secret in production).
    monkeypatch.setenv("GROCY_MCP_AUTH__OIDC_CLIENT_ID", "grocy-mcp-sf")
    monkeypatch.setenv("GROCY_MCP_AUTH__OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("GROCY_MCP_AUTH__PROXY_CLIENT_ID", "grocy-sf")

    settings = ServerSettings()
    assert settings.grocy_url == "https://grocy-sf.example.com"
    assert settings.persistence == ValkeyPersistence(kind="valkey", host="valkey.example.com", db=0)
    assert settings.auth is not None
    # from YAML:
    assert settings.auth.oidc_issuer == "https://auth.example.com/application/o/grocy-mcp-sf/"
    assert settings.auth.extra_jwt_issuers == ("https://auth.example.com/application/o/grocy-mcp-haku-sf/",)
    # from env (the secret):
    assert settings.auth.oidc_client_secret == "shh"
    assert settings.auth.proxy_client_id == "grocy-sf"


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

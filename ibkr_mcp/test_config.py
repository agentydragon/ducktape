"""ServerSettings: defaults, and the YAML-file / env-secret split the deployment relies on."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import pytest_bazel

from ibkr_mcp.mcp_types import ServerSettings
from mcp_infra.persistence import PostgresPersistence, ValkeyPersistence


def test_defaults_are_localhost_gateway_no_auth() -> None:
    settings = ServerSettings()
    assert settings.auth is None
    assert settings.gateway_base_url == "https://localhost:5000/v1/api"
    assert settings.gateway_verify_tls is False
    assert settings.port == 8765


def test_yaml_config_deep_merges_with_env_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-secret config from IBKR_MCP_CONFIG_FILE (YAML) deep-merges with env, so the `auth`
    model draws issuer/URLs/direct_jwt_trusts from the file and its secret from env — the split
    the k8s deployment relies on."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent("""
        gateway_base_url: https://localhost:5000/v1/api
        auth:
          oidc_issuer: https://auth.example.com/application/o/ibkr-mcp/
          public_base_url: https://ibkr-mcp.example.com
          direct_jwt_trusts:
            - issuer: https://auth.example.com/application/o/ibkr-mcp-haku/
              audiences: [ibkr-mcp-haku]
              required_scopes: [openid]
        persistence:
          kind: valkey
          host: valkey.example.com
          db: 0
        """)
    )
    monkeypatch.setenv("IBKR_MCP_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("IBKR_MCP_AUTH__OIDC_CLIENT_ID", "ibkr-mcp")
    monkeypatch.setenv("IBKR_MCP_AUTH__OIDC_CLIENT_SECRET", "shh")

    settings = ServerSettings()
    assert settings.persistence == ValkeyPersistence(kind="valkey", host="valkey.example.com", db=0)
    assert settings.auth is not None
    assert settings.auth.oidc_issuer == "https://auth.example.com/application/o/ibkr-mcp/"
    assert settings.auth.direct_jwt_trusts[0].audiences == ("ibkr-mcp-haku",)
    assert settings.auth.oidc_client_secret == "shh"


def test_postgres_persistence_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment supplies the OAuth store as postgres via env (KIND + URL from
    the CNPG app Secret); pin that it parses to PostgresPersistence."""
    monkeypatch.setenv("IBKR_MCP_PERSISTENCE__KIND", "postgres")
    monkeypatch.setenv("IBKR_MCP_PERSISTENCE__URL", "postgresql://u:p@ibkr-mcp-db-rw:5432/oauth_store")

    settings = ServerSettings()
    assert settings.persistence == PostgresPersistence(
        kind="postgres", url="postgresql://u:p@ibkr-mcp-db-rw:5432/oauth_store"
    )


if __name__ == "__main__":
    pytest_bazel.main()

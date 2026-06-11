from __future__ import annotations

import pytest_bazel

from mcp_infra.authentik_auth.auth import AuthentikAuthConfig
from mcp_infra.oauth_facade.config import FacadeSettings, HttpUpstream, StdioUpstream


def _auth() -> AuthentikAuthConfig:
    return AuthentikAuthConfig(
        oidc_issuer="https://auth.example.com/application/o/test/",
        oidc_client_id="id",
        oidc_client_secret="secret",
        public_base_url="https://test.example.com",
    )


def test_http_upstream_settings() -> None:
    settings = FacadeSettings(
        auth=_auth(),
        upstream=HttpUpstream(url="http://upstream.svc:8080/mcp", bearer_token="pat"),
        facade_name="Test Facade",
    )
    assert isinstance(settings.upstream, HttpUpstream)
    assert settings.upstream.url == "http://upstream.svc:8080/mcp"
    assert settings.upstream.bearer_token == "pat"


def test_stdio_upstream_settings() -> None:
    settings = FacadeSettings(
        auth=_auth(),
        upstream=StdioUpstream(command=["/upstream/node", "/upstream/build/index.js"]),
        facade_name="Test Facade",
    )
    assert isinstance(settings.upstream, StdioUpstream)
    assert settings.upstream.command == ["/upstream/node", "/upstream/build/index.js"]


def test_env_loading_http(monkeypatch) -> None:
    monkeypatch.setenv("MCP_FACADE_AUTH__OIDC_ISSUER", "https://auth.example.com/application/o/test/")
    monkeypatch.setenv("MCP_FACADE_AUTH__OIDC_CLIENT_ID", "id")
    monkeypatch.setenv("MCP_FACADE_AUTH__OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_FACADE_AUTH__PUBLIC_BASE_URL", "https://test.example.com")
    monkeypatch.setenv("MCP_FACADE_UPSTREAM__KIND", "http")
    monkeypatch.setenv("MCP_FACADE_UPSTREAM__URL", "http://upstream.svc:8080/mcp")
    monkeypatch.setenv("MCP_FACADE_FACADE_NAME", "Test Facade")
    settings = FacadeSettings()
    assert isinstance(settings.upstream, HttpUpstream)
    assert settings.upstream.url == "http://upstream.svc:8080/mcp"
    assert settings.facade_name == "Test Facade"


def test_env_loading_stdio(monkeypatch) -> None:
    monkeypatch.setenv("MCP_FACADE_AUTH__OIDC_ISSUER", "https://auth.example.com/application/o/test/")
    monkeypatch.setenv("MCP_FACADE_AUTH__OIDC_CLIENT_ID", "id")
    monkeypatch.setenv("MCP_FACADE_AUTH__OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_FACADE_AUTH__PUBLIC_BASE_URL", "https://test.example.com")
    monkeypatch.setenv("MCP_FACADE_UPSTREAM__KIND", "stdio")
    monkeypatch.setenv("MCP_FACADE_UPSTREAM__COMMAND", '["/upstream/node","/upstream/build/index.js"]')
    monkeypatch.setenv("MCP_FACADE_FACADE_NAME", "Test Facade")
    settings = FacadeSettings()
    assert isinstance(settings.upstream, StdioUpstream)
    assert settings.upstream.command == ["/upstream/node", "/upstream/build/index.js"]


if __name__ == "__main__":
    pytest_bazel.main()

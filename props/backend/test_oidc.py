"""OIDC settings and dashboard session discovery."""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from props.backend.app import BackendDeps, create_app
from props.backend.oidc import load_oidc_settings
from props.config import PropsConfig
from props.core.oci_utils import RegistryProxyConfig

_FULL_ENV = {
    "PROPS_OIDC_ISSUER": "https://auth.example.test/application/o/props/",
    "PROPS_OIDC_CLIENT_ID": "props",
    "PROPS_OIDC_CLIENT_SECRET": "shh",
    "PROPS_OIDC_SESSION_SECRET": "cookie-signing-key",
    "PROPS_OIDC_PUBLIC_BASE_URL": "https://props.example.test",
    "PROPS_OIDC_ADMIN_EMAILS": "admin@test.invalid, other@example.com",
}


@pytest.fixture
def oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _FULL_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize(("sso_enabled", "status"), [(True, 401), (False, 404)])
async def test_dashboard_session_probe(
    oidc_env: None, monkeypatch: pytest.MonkeyPatch, sso_enabled: bool, status: int
) -> None:
    if not sso_enabled:
        monkeypatch.delenv("PROPS_OIDC_ISSUER")
    deps = BackendDeps(
        config=PropsConfig(backend_url="http://backend.test", agent_env={}),
        registry_proxy_config=RegistryProxyConfig(host="registry.test", port=5000),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(deps=deps)), base_url="http://backend.test"
    ) as client:
        response = await client.get("/auth/me")
    assert response.status_code == status


def test_redirect_uri_and_metadata_url(oidc_env: None) -> None:
    settings = load_oidc_settings()
    assert settings is not None
    assert settings.redirect_uri == "https://props.example.test/auth/callback"
    assert settings.server_metadata_url == (
        "https://auth.example.test/application/o/props/.well-known/openid-configuration"
    )


@pytest.mark.parametrize(("url", "secure"), [("https://props.example.test", True), ("http://localhost:8000", False)])
def test_cookie_secure_follows_scheme(oidc_env: None, monkeypatch: pytest.MonkeyPatch, url: str, secure: bool) -> None:
    monkeypatch.setenv("PROPS_OIDC_PUBLIC_BASE_URL", url)
    settings = load_oidc_settings()
    assert settings is not None
    assert settings.cookie_secure is secure


def test_is_admin_matches_allowlist_case_insensitively(oidc_env: None) -> None:
    settings = load_oidc_settings()
    assert settings is not None
    assert settings.is_admin("admin@test.invalid")
    assert settings.is_admin("Admin@Test.Invalid")
    assert settings.is_admin("other@example.com")
    assert not settings.is_admin("intruder@test.invalid")


if __name__ == "__main__":
    pytest_bazel.main()

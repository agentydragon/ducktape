"""Unit tests for OIDC settings (no DB / Docker required)."""

from __future__ import annotations

import pytest
import pytest_bazel

from props.backend.oidc import OIDCSettings, load_oidc_settings

_FULL_ENV = {
    "PROPS_OIDC_ISSUER": "https://auth.allegedly.works/application/o/props/",
    "PROPS_OIDC_CLIENT_ID": "props",
    "PROPS_OIDC_CLIENT_SECRET": "shh",
    "PROPS_OIDC_SESSION_SECRET": "cookie-signing-key",
    "PROPS_OIDC_PUBLIC_BASE_URL": "https://props.allegedly.works",
    "PROPS_OIDC_ADMIN_EMAILS": "agentydragon@gmail.com, other@example.com",
}


@pytest.fixture
def oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _FULL_ENV.items():
        monkeypatch.setenv(key, value)


def test_load_returns_none_when_issuer_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROPS_OIDC_ISSUER", raising=False)
    assert load_oidc_settings() is None


def test_load_returns_settings_when_configured(oidc_env: None) -> None:
    settings = load_oidc_settings()
    assert settings is not None
    assert settings.client_id == "props"


def test_redirect_uri_and_metadata_url(oidc_env: None) -> None:
    settings = load_oidc_settings()
    assert settings is not None
    assert settings.redirect_uri == "https://props.allegedly.works/auth/callback"
    assert settings.server_metadata_url == (
        "https://auth.allegedly.works/application/o/props/.well-known/openid-configuration"
    )


def test_cookie_secure_follows_scheme() -> None:
    https = OIDCSettings(
        issuer="x",
        client_id="x",
        client_secret="x",
        session_secret="x",
        admin_emails="a@b",
        public_base_url="https://props.allegedly.works",
    )
    http = OIDCSettings(
        issuer="x",
        client_id="x",
        client_secret="x",
        session_secret="x",
        admin_emails="a@b",
        public_base_url="http://localhost:8000",
    )
    assert https.cookie_secure
    assert not http.cookie_secure


def test_is_admin_matches_allowlist_case_insensitively(oidc_env: None) -> None:
    settings = load_oidc_settings()
    assert settings is not None
    assert settings.is_admin("agentydragon@gmail.com")
    assert settings.is_admin("AgentyDragon@Gmail.com")
    assert settings.is_admin("other@example.com")
    assert not settings.is_admin("intruder@evil.com")


if __name__ == "__main__":
    pytest_bazel.main()

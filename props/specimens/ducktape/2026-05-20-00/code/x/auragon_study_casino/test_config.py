"""Settings tests that go through the env-source path.

The bulk of admin-related tests construct `Settings(admin_users={...})`
directly, which bypasses pydantic-settings' env parser entirely. These
tests cover the env-source path so we don't regress on the production
config style (`STUDY_CASINO_ADMIN_USERS=...`).
"""

from __future__ import annotations

import pytest
import pytest_bazel

from x.auragon_study_casino.config import Settings

_DUMMY_URL = "postgresql+psycopg://u:p@host/db"


def test_admin_users_from_env_single(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDY_CASINO_DATABASE_URL", _DUMMY_URL)
    monkeypatch.setenv("STUDY_CASINO_ADMIN_USERS", "agentydragon")
    assert Settings().admin_users == frozenset({"agentydragon"})


def test_admin_users_from_env_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDY_CASINO_DATABASE_URL", _DUMMY_URL)
    monkeypatch.setenv("STUDY_CASINO_ADMIN_USERS", "rai, auragon ,foo")
    assert Settings().admin_users == frozenset({"rai", "auragon", "foo"})


def test_admin_users_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDY_CASINO_DATABASE_URL", _DUMMY_URL)
    monkeypatch.setenv("STUDY_CASINO_ADMIN_USERS", "")
    assert Settings().admin_users == frozenset()


def test_admin_users_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDY_CASINO_DATABASE_URL", _DUMMY_URL)
    monkeypatch.delenv("STUDY_CASINO_ADMIN_USERS", raising=False)
    assert Settings().admin_users == frozenset()


def test_rng_secret_required_when_oidc_is_enabled() -> None:
    settings = Settings(
        database_url=_DUMMY_URL,
        oidc_issuer="https://auth.example.test/application/o/study-casino",
        oidc_client_id="study-casino",
        oidc_client_secret="client-secret",
        session_secret="x" * 32,
    )

    with pytest.raises(ValueError, match="STUDY_CASINO_RNG_SECRET"):
        settings.rng_secret_bytes()


def test_rng_secret_dev_fallback_when_oidc_is_disabled() -> None:
    settings = Settings(database_url=_DUMMY_URL)

    assert settings.effective_rng_key_id() == "dev-insecure-fallback"
    assert settings.rng_secret_bytes().startswith(b"insecure-dev-only")


if __name__ == "__main__":
    pytest_bazel.main()

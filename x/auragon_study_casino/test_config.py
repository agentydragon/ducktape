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


if __name__ == "__main__":
    pytest_bazel.main()

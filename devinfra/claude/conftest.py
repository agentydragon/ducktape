"""Pytest fixtures for Claude tests."""

from pathlib import Path

import pytest


@pytest.fixture
def patch_credentials_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch CREDENTIALS_PATH to a custom path for testing."""
    path = tmp_path / "credentials.json"
    monkeypatch.setattr("devinfra.claude.claude_api.credentials.CREDENTIALS_PATH", path)
    return path


@pytest.fixture
def no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch CREDENTIALS_PATH to a nonexistent file (no cached credentials)."""
    monkeypatch.setattr("devinfra.claude.claude_api.credentials.CREDENTIALS_PATH", tmp_path / "nonexistent")

"""Persistence and verification behavior for reusable session storage."""

import pytest
import pytest_bazel
from pydantic import BaseModel, TypeAdapter

from platform_sessions.session_store import ProviderSessionStore


class Session(BaseModel):
    token: str


def test_install_verifies_before_replacing_and_keeps_private_file(tmp_path):
    def verify(candidate, *, transport=None):
        if candidate.token == "bad":
            raise ValueError("rejected")

    store = ProviderSessionStore(tmp_path / "state", "session.json", TypeAdapter(Session), verify)
    store.install(Session(token="good"))
    before = store.snapshot()
    with pytest.raises(ValueError, match="rejected"):
        store.install(Session(token="bad"))
    assert store.snapshot() == before
    assert store.load() == Session(token="good")
    assert store.path.stat().st_mode & 0o077 == 0
    assert store.directory.stat().st_mode & 0o077 == 0


def test_rejects_insecure_or_invalid_session_file(tmp_path):
    store = ProviderSessionStore(tmp_path, "session.json", TypeAdapter(Session), lambda *_args, **_kwargs: None)
    assert store.load() is None
    store.path.write_text("{}")
    store.path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        store.load()
    store.path.chmod(0o600)
    with pytest.raises(ValueError, match="Invalid stored session"):
        store.load()


if __name__ == "__main__":
    pytest_bazel.main()

"""Verify probe failures do not leak stored session data."""

import pytest_bazel
from pydantic import BaseModel, TypeAdapter

from platform_sessions.probe import probe_session
from platform_sessions.session_store import ProviderSessionStore


class Session(BaseModel):
    token: int


def test_invalid_stored_session_does_not_log_input(tmp_path, caplog):
    store = ProviderSessionStore(tmp_path, "session.json", TypeAdapter(Session))
    store.path.write_text('{"token":"private-invalid-value"}')
    store.path.chmod(0o600)
    event = probe_session(store, checker=lambda *_args, **_kwargs: None, provider="example")
    assert event["outcome"] == "storage_error"
    assert "private-invalid-value" not in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()

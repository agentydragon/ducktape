"""Verify probe failures do not leak stored session data."""

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import update

from platform_sessions.probe import probe_session
from platform_sessions.probe_types import ProbeOutcome
from platform_sessions.session_store import ProviderSessionStore, _sessions


class Session(BaseModel):
    token: int


def test_invalid_stored_session_does_not_log_input(tmp_path, caplog):
    store = ProviderSessionStore(tmp_path, "session.json", TypeAdapter(Session))
    with store.storage.transaction() as connection:
        connection.execute(update(_sessions).values(credentials=b'{"token":"private-invalid-value"}'))
    event = probe_session(store, checker=lambda *_args, **_kwargs: None, provider="example")
    assert event.outcome is ProbeOutcome.STORAGE_ERROR
    assert event.as_event()["outcome"] == "storage_error"
    assert "private-invalid-value" not in caplog.text


def test_superseded_probe_returns_typed_result_without_logging(tmp_path, caplog):
    store = ProviderSessionStore(tmp_path, "session.json", TypeAdapter(Session))
    store.save(Session(token=1))

    def replace_session(_candidate, *, transport=None):
        store.save(Session(token=2))

    result = probe_session(store, checker=replace_session, provider="example")

    assert result.outcome is ProbeOutcome.SUPERSEDED
    assert result.as_event()["outcome"] == "superseded"
    assert "session_probe" not in caplog.text

"""The record on disk is what a re-attaching Open compares its spec against."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_bazel

from x.agentplane.runner import protocol_pb2
from x.agentplane.runner.store import SessionRecord, SessionStore
from x.agentplane.runner.testing.storage_image import StorageImage

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


def test_a_stored_record_reproduces_the_spec_it_was_created_from(tmp_path: Path) -> None:
    spec = protocol_pb2.SessionSpec(
        harness=protocol_pb2.HARNESS_CLAUDE,
        cwd="/session/workspace",
        model="test-backend/test-model",
        reasoning_effort="low",
        instructions="Standing order for this session: the operator's name is Wren.",
    )
    store = SessionStore(tmp_path)
    store.write("session-1", SessionRecord.from_spec(spec))
    assert store.read("session-1").spec() == spec


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StorageImage:
    root = tmp_path / "volume"
    root.mkdir()
    image = StorageImage(root)
    monkeypatch.setattr(os, "fsync", image.fsync)
    return image


def test_new_session_and_replaced_metadata_remain_findable(storage: StorageImage, tmp_path: Path) -> None:
    storage.fail_path = storage.root
    with pytest.raises(OSError, match="injected storage fence failure"):
        SessionStore(storage.root / "state" / "sessions")
    storage.fail_path = None
    store = SessionStore(storage.root / "state" / "sessions")
    record = SessionRecord.from_spec(protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CLAUDE, model="test-model"))
    store.write("test-session", record)
    record.native_session_id = "test-native-session"
    store.write("test-session", record)

    recovered_root = tmp_path / "recovered"
    storage.recover(recovered_root)
    recovered_store = SessionStore(recovered_root / "state" / "sessions")
    assert recovered_store.session_ids() == ["test-session"]
    assert recovered_store.read("test-session") == record


if __name__ == "__main__":
    pytest_bazel.main()

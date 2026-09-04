"""The record on disk is what a re-attaching Open compares its spec against."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.store import SessionRecord, SessionStore

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


def test_a_stored_record_reproduces_the_spec_it_was_created_from(tmp_path: Path) -> None:
    spec = pb.SessionSpec(
        provider=pb.PROVIDER_CLAUDE,
        cwd="/session/workspace",
        model="test-provider/test-model",
        reasoning_effort="low",
        instructions="Standing order for this session: the operator's name is Wren.",
    )
    store = SessionStore(tmp_path)
    store.write("session-1", SessionRecord.from_spec(spec))
    assert store.read("session-1").spec() == spec


if __name__ == "__main__":
    pytest_bazel.main()

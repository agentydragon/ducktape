"""Assertions over VM trace artifacts retained from the process-trace receiver."""

import json
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.system_tests.vm.trace_contract import _codex_session_uids, _read_jsonl


def test_driver_session_create_requires_ready_sandbox_identity() -> None:
    session_create: dict[str, object] = {
        "name": "system_test.session.create",
        "attributes": {"harness": "HARNESS_CODEX", "sandbox.name": "missing-ready-span"},
    }
    with pytest.raises(AssertionError, match="has no ready identity"):
        _codex_session_uids([session_create], {})


def test_jsonl_reader_ignores_only_an_incomplete_trailing_record(tmp_path: Path) -> None:
    path = tmp_path / "spans.jsonl"
    path.write_bytes(b'{"name":"complete"}\n{"name":"partial"')
    assert _read_jsonl(path) == [{"name": "complete"}]

    path.write_bytes(b'{"name": }')
    with pytest.raises(json.JSONDecodeError):
        _read_jsonl(path)


if __name__ == "__main__":
    pytest_bazel.main()

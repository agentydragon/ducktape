import json
from unittest.mock import patch

import pytest
import pytest_bazel
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.ingest import (
    ArchiveSource,
    HarnessRoot,
    SnapshotIngestor,
    _extract_session_metadata,
    _remember_session_kind,
)
from x.ai_session_archive.models import Harness, SessionKind
from x.ai_session_archive.record_json import ParsedRecordJson, parse_record_json
from x.ai_session_archive.restic import ResticChange, ResticRepository


def test_unit_restic_question_modifier_is_an_indexable_content_change() -> None:
    repository = ResticRepository(binary="unused", environment={})
    path = "/home/archive/.claude/projects/project/session.jsonl"
    with patch.object(
        repository,
        "_run",
        return_value=b'{"message_type":"change","path":"/home/archive/.claude/projects/project/session.jsonl","modifier":"?"}\n',
    ):
        changes = repository.diff_snapshots("before", "after")
    assert changes == [ResticChange(path=path, modifier="?")]

    source = ArchiveSource(
        machine_id="question-marker-test",
        display_name="Question marker test",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, "/home/archive/.claude"),),
    )
    ingestor = SnapshotIngestor(sessionmaker(), source)
    entries = ingestor._changed_entries(changes)
    assert entries[0][5] == "pending"
    assert entries[0][1] == "?"


@pytest.mark.parametrize("timestamp", ["2026-01-01T00:00:00", "not-a-timestamp"])
def test_unit_timezone_naive_or_invalid_record_timestamps_are_unknown(timestamp: str) -> None:
    record = parse_record_json(json.dumps({"timestamp": timestamp}).encode())
    assert isinstance(record, ParsedRecordJson)
    assert record.timestamp is None


def test_unit_codex_parent_id_requires_session_meta_record() -> None:
    metadata: dict[str, object] = {}
    _extract_session_metadata(
        {"type": "response_item", "payload": {"parent_thread_id": "not-a-parent", "id": "not-a-session"}},
        metadata,
        timestamp=None,
        harness=Harness.CODEX,
    )
    assert "parent_native_session_id" not in metadata
    assert "native_session_id" not in metadata


@pytest.mark.parametrize(
    ("harness", "record", "expected"),
    [
        (Harness.CODEX, {"type": "session_meta", "payload": {"source": "cli"}}, SessionKind.MAIN),
        (
            Harness.CODEX,
            {"type": "session_meta", "payload": {"source": {"subagent": {"other": "guardian"}}}},
            SessionKind.GUARDIAN,
        ),
        (
            Harness.CODEX,
            {"type": "session_meta", "payload": {"source": {"subagent": {"thread_spawn": {}}}}},
            SessionKind.SUBAGENT,
        ),
        (Harness.CODEX, {"type": "response_item", "payload": {"source": "cli"}}, SessionKind.UNKNOWN),
        (Harness.CLAUDE, {"type": "user", "isSidechain": True}, SessionKind.SUBAGENT),
        (Harness.CLAUDE, {"type": "user", "isSidechain": False}, SessionKind.MAIN),
        (Harness.CLAUDE, {"type": "user", "isSidechain": "true"}, SessionKind.UNKNOWN),
    ],
)
def test_unit_session_kind_uses_harness_metadata(
    harness: Harness, record: dict[str, object], expected: SessionKind
) -> None:
    metadata: dict[str, object] = {}
    _extract_session_metadata(record, metadata, timestamp=None, harness=harness)
    assert metadata.get("session_kind", SessionKind.UNKNOWN.value) == expected.value


def test_unit_unrecognized_later_source_does_not_erase_known_session_kind() -> None:
    metadata: dict[str, object] = {"session_kind": SessionKind.MAIN.value}
    _remember_session_kind(metadata, SessionKind.UNKNOWN)
    assert metadata["session_kind"] == SessionKind.MAIN.value


def test_unit_unrecognized_source_can_be_refined_by_later_known_source() -> None:
    metadata: dict[str, object] = {}
    _remember_session_kind(metadata, SessionKind.UNKNOWN)
    _remember_session_kind(metadata, SessionKind.SUBAGENT)
    assert metadata["session_kind"] == SessionKind.SUBAGENT.value


if __name__ == "__main__":
    pytest_bazel.main()

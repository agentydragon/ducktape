import json
from unittest.mock import patch

import pytest
import pytest_bazel
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor, _extract_session_metadata
from x.ai_session_archive.models import Harness
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
    )
    assert "parent_native_session_id" not in metadata
    assert "native_session_id" not in metadata


if __name__ == "__main__":
    pytest_bazel.main()

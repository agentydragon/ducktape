"""Failure capture reads a committed, bounded prefix without changing the live journal."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest_bazel

from agentplane.env.e2e.testing.failure_journal import FailureEvidence
from agentplane.proto import event_log_pb2


def test_capture_retained_journals_while_writer_has_uncommitted_event(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    journal = state_dir / "sessions" / "session-1" / "journal.sqlite"
    journal.parent.mkdir(parents=True)
    with closing(sqlite3.connect(journal)) as writer:
        writer.execute("CREATE TABLE event_entry (cursor INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
        writer.executemany(
            "INSERT INTO event_entry VALUES (?, ?)",
            [(cursor, event_log_pb2.EventEntry(cursor=cursor).SerializeToString()) for cursor in range(1, 601)],
        )
        writer.commit()
        writer.execute(
            "INSERT INTO event_entry VALUES (?, ?)", (601, event_log_pb2.EventEntry(cursor=601).SerializeToString())
        )
        evidence = FailureEvidence(tmp_path / "evidence")
        evidence.capture_retained_journals(state_dir)
        captured = json.loads((evidence.directory / "retained-journals.json").read_text())
        assert [int(entry["cursor"]) for entry in captured["session-1"]] == list(range(89, 601))
        assert writer.in_transaction
        writer.rollback()
        assert writer.execute("SELECT MAX(cursor) FROM event_entry").fetchone() == (600,)


if __name__ == "__main__":
    pytest_bazel.main()

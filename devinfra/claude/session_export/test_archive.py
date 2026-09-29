import gzip
import json
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.claude.session_export.api import SessionsApi
from devinfra.claude.session_export.archive import (
    ManifestRecord,
    export_all,
    export_session,
    sessions_to_export,
    verify_archive,
)
from devinfra.claude.session_export.conftest import FakeSessionsService, make_events
from devinfra.claude.session_export.models import SessionSummary

SESSION_ID = "session_test0001"


def read_lines(path: Path) -> list[str]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read().splitlines()


async def test_export_session_archives_every_event_losslessly(
    service: FakeSessionsService, api: SessionsApi, tmp_path: Path
) -> None:
    events = make_events(1203)
    service.events[SESSION_ID] = events
    # A `cse_` id names the same session as the `session_` one the archive files use.
    record = await export_session(api, SessionSummary(id="cse_test0001", last_event_at="test-time"), tmp_path)
    assert [json.loads(line) for line in read_lines(tmp_path / f"{SESSION_ID}.jsonl.gz")] == events
    assert record.event_count == 1203
    assert record.is_complete


async def test_export_session_rejects_a_sequence_gap_and_leaves_no_finished_file(
    service: FakeSessionsService, api: SessionsApi, tmp_path: Path
) -> None:
    events = make_events(5)
    del events[2]
    service.events[SESSION_ID] = events
    with pytest.raises(ValueError, match="expected sequence_num 3, got 4"):
        await export_session(api, SessionSummary(id=SESSION_ID, last_event_at="test-time"), tmp_path)
    assert not (tmp_path / f"{SESSION_ID}.jsonl.gz").exists()


def test_sessions_to_export_skips_only_complete_unchanged_sessions() -> None:
    def record(name: str, *, last_event_at: str = "t1", newest: int = 5) -> ManifestRecord:
        return ManifestRecord(
            session_id=f"session_{name}",
            last_event_at=last_event_at,
            event_count=5,
            newest_sequence_num=newest,
            gz_bytes=1,
        )

    manifest = {
        "session_done": record("done"),
        "session_live": record("live", newest=6),
        "session_changed": record("changed", last_event_at="t0"),
    }
    sessions = [SessionSummary(id=f"cse_{name}", last_event_at="t1") for name in ("done", "live", "changed", "new")]
    assert [s.id for s in sessions_to_export(sessions, manifest)] == ["cse_live", "cse_changed", "cse_new"]


async def test_export_all_is_resumable(service: FakeSessionsService, api: SessionsApi, tmp_path: Path) -> None:
    service.events = {f"session_test{i:04d}": make_events(n) for i, n in enumerate((3, 700, 0))}
    await export_all(api, tmp_path, workers=2, session_ids=None, limit=None)
    event_requests = [r for r in service.requests if r.url.path.endswith("/events")]

    await export_all(api, tmp_path, workers=2, session_ids=None, limit=None)
    assert [r for r in service.requests if r.url.path.endswith("/events")] == event_requests


async def test_verify_accepts_a_fresh_archive_and_flags_a_truncated_file(
    service: FakeSessionsService, api: SessionsApi, tmp_path: Path
) -> None:
    service.events = {f"session_test{i:04d}": make_events(n) for i, n in enumerate((3, 700, 0))}
    await export_all(api, tmp_path, workers=2, session_ids=None, limit=None)
    verify_archive(tmp_path)

    victim = tmp_path / "events" / "session_test0001.jsonl.gz"
    kept = read_lines(victim)[:-1]
    with gzip.open(victim, "wt", encoding="utf-8") as f:
        f.write("".join(f"{line}\n" for line in kept))
    with pytest.raises(ValueError, match="session_test0001"):
        verify_archive(tmp_path)


if __name__ == "__main__":
    pytest_bazel.main()

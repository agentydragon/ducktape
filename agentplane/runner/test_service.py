"""Runner lifetime owns database connections, including failed harness shutdown, and its session
summaries describe the published log."""

from pathlib import Path

import pytest
import pytest_bazel

from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.config import RunnerConfig
from agentplane.runner.service import Runner
from agentplane.runner.store import SessionRecord, StateOwner, StateOwnershipError

# gazelle:include_dep @pypi//protobuf


async def test_failed_session_shutdown_still_closes_its_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(RunnerConfig(state_dir=tmp_path, environment={}))
    runner.store.write(
        "test-session",
        SessionRecord.from_spec(protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CLAUDE, model="test-model")),
    )
    session = await runner._load("test-session")

    async def failed_stop() -> None:
        raise OSError("test harness shutdown failure")

    monkeypatch.setattr(session, "stop", failed_stop)
    with pytest.raises(OSError, match="test harness shutdown failure"):
        await runner.stop()
    assert session.journal._connection.closed
    with pytest.raises(StateOwnershipError):
        StateOwner(tmp_path)
    runner._state_owner.close()


async def test_summaries_report_the_published_log_not_a_batch_in_progress(tmp_path: Path) -> None:
    runner = Runner(RunnerConfig(state_dir=tmp_path, environment={}))
    runner.store.write(
        "test-session",
        SessionRecord.from_spec(protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CLAUDE, model="test-model")),
    )
    session = await runner._load("test-session")
    try:
        async with session.journal.batch():
            await session.emit(event_pb2.TurnStarted(turn_id="test-turn"))
            (summary,) = runner.summaries()
            assert (summary.last_cursor, summary.active_turn_id) == (0, "")
        (summary,) = runner.summaries()
        assert (summary.last_cursor, summary.active_turn_id) == (1, "test-turn")
    finally:
        await runner.stop()


if __name__ == "__main__":
    pytest_bazel.main()

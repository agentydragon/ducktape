"""Runner lifetime owns database connections, including failed harness shutdown."""

from pathlib import Path

import pytest
import pytest_bazel

from x.agentplane.runner import protocol_pb2
from x.agentplane.runner.config import RunnerConfig
from x.agentplane.runner.service import Runner
from x.agentplane.runner.store import SessionRecord

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


if __name__ == "__main__":
    pytest_bazel.main()

"""Runner lifetime owns database connections, a harness launch that fails says why, and session
summaries describe the published log."""

import textwrap
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.config import ClaudeLaunch, CodexLaunch, RunnerConfig
from agentplane.runner.errors import RunnerError
from agentplane.runner.service import Runner
from agentplane.runner.store import SessionRecord, StateOwner, StateOwnershipError

# gazelle:include_dep @pypi//protobuf

REFUSAL = "test harness: refusing the handshake on purpose"
# Reads the handshake's first request before dying, so the failure lands mid-handshake.
FAILING_HARNESS = textwrap.dedent(f"""\
    #!/bin/sh
    read -r request
    echo "{REFUSAL}" >&2
    exit 3
""")
TEST_ENDPOINT = "http://agentplane-test-endpoint.invalid"


@pytest.fixture
def harness_binary(tmp_path: Path) -> Path:
    path = tmp_path / "failing-harness"
    path.write_text(FAILING_HARNESS)
    path.chmod(0o755)
    return path


@pytest.fixture
def config(harness: protocol_pb2.Harness, harness_binary: Path, tmp_path: Path) -> RunnerConfig:
    """Overrides the package fixture: the session's harness is `harness_binary`, not the pinned one."""
    return RunnerConfig(
        state_dir=tmp_path / "state",
        native_state_dir=tmp_path / "native-state",
        claude=ClaudeLaunch(binary=harness_binary, base_url=TEST_ENDPOINT, auth_token="test-token")
        if harness == protocol_pb2.HARNESS_CLAUDE
        else None,
        codex=CodexLaunch(binary=harness_binary, base_url=f"{TEST_ENDPOINT}/v1", api_key="test-token")
        if harness == protocol_pb2.HARNESS_CODEX
        else None,
    )


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


async def test_a_harness_that_dies_in_its_handshake_names_its_exit_and_stderr(
    client: RunnerClient, config: RunnerConfig, harness: protocol_pb2.Harness, spec: protocol_pb2.SessionSpec
) -> None:
    with pytest.raises(RunnerError) as raised:
        await client.attach("launch-failure-1", spec=spec)
    assert "exit_code=3" in str(raised.value)
    assert REFUSAL in str(raised.value)
    assert config.native_state_dir is not None
    native_home = "claude" if harness == protocol_pb2.HARNESS_CLAUDE else "codex"
    assert (config.native_state_dir / "launch-failure-1" / native_home).is_dir()


@pytest.mark.parametrize("harness_binary", [Path("/nonexistent/agentplane-test-harness")])
async def test_a_harness_the_supervisor_cannot_start_names_the_spawn_failure(
    client: RunnerClient, spec: protocol_pb2.SessionSpec
) -> None:
    with pytest.raises(RunnerError) as raised:
        await client.attach("launch-failure-2", spec=spec)
    assert "exit_code=125" in str(raised.value)
    assert "harness supervisor: start native harness" in str(raised.value)


async def test_summaries_report_the_published_log_not_a_batch_in_progress(tmp_path: Path) -> None:
    runner = Runner(RunnerConfig(state_dir=tmp_path, environment={}))
    runner.store.write(
        "test-session",
        SessionRecord.from_spec(protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CLAUDE, model="test-model")),
    )
    session = await runner._load("test-session")
    try:
        async with session.journal.batch():
            await session.emit(event_pb2.TurnStarted(turn_id="test-turn"), sources=[])
            (summary,) = runner.summaries()
            assert (summary.last_cursor, summary.active_turn_id) == (0, "")
        (summary,) = runner.summaries()
        assert (summary.last_cursor, summary.active_turn_id) == (1, "test-turn")
    finally:
        await runner.stop()


async def test_vm_workspace_root_rejects_outside_paths_and_symlink_escapes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    runner = Runner(RunnerConfig(state_dir=tmp_path / "state", workspace_root=workspace))
    try:
        runner._validate_workspace(str(workspace / "new-session"))
        with pytest.raises(ValueError, match="must be a child"):
            runner._validate_workspace(str(outside / "session"))
        with pytest.raises(ValueError, match="must be a child"):
            runner._validate_workspace(str(workspace / "escape" / "session"))
    finally:
        await runner.stop()


if __name__ == "__main__":
    pytest_bazel.main()

"""Runner lifetime owns database connections, a harness launch that fails says why, and session
summaries describe the published log."""

import textwrap
from pathlib import Path

import grpc
import pytest
import pytest_bazel

from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.config import ClaudeLaunch, CodexLaunch, RunnerConfig
from agentplane.runner.runner import Runner
from agentplane.runner.store import SessionRecord, StateOwner, StateOwnershipError
from agentplane.runner.testing import launches
from agentplane.runner.testing.driver import RunnerDriver

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
def config(harness: runner_pb2.Harness, harness_binary: Path, tmp_path: Path) -> RunnerConfig:
    """Overrides the package fixture: the session's harness is `harness_binary`, not the pinned one."""
    return RunnerConfig(
        state_dir=tmp_path / "state",
        launch=launches.SANDBOX_LAUNCH,
        model_config_resolver=launches.unconfigured_models(),
        claude=ClaudeLaunch(binary=harness_binary, base_url=TEST_ENDPOINT, auth_token="test-token")
        if harness == runner_pb2.HARNESS_CLAUDE
        else None,
        codex=CodexLaunch(binary=harness_binary, base_url=f"{TEST_ENDPOINT}/v1", api_key="test-token")
        if harness == runner_pb2.HARNESS_CODEX
        else None,
    )


async def test_failed_session_shutdown_still_closes_its_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(RunnerConfig(state_dir=tmp_path, launch=launches.SANDBOX_LAUNCH, harness_environment={}))
    runner.store.write(
        "test-session",
        SessionRecord.from_spec(runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, model="test-model")),
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
    driver: RunnerDriver, config: RunnerConfig, harness: runner_pb2.Harness, spec: runner_pb2.SessionSpec
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await driver.attach("launch-failure-1", spec=spec)
    assert raised.value.code() == grpc.StatusCode.UNKNOWN
    assert "exit_code=3" in (details := raised.value.details() or "")
    assert REFUSAL in details
    native_home = "claude" if harness == runner_pb2.HARNESS_CLAUDE else "codex"
    assert (config.state_dir / "sessions" / "launch-failure-1" / native_home).is_dir()


@pytest.mark.parametrize("harness_binary", [Path("/nonexistent/agentplane-test-harness")])
async def test_a_harness_the_supervisor_cannot_start_names_the_spawn_failure(
    driver: RunnerDriver, spec: runner_pb2.SessionSpec
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        await driver.attach("launch-failure-2", spec=spec)
    assert raised.value.code() == grpc.StatusCode.UNKNOWN
    assert "exit_code=125" in (details := raised.value.details() or "")
    assert "harness supervisor: start native harness" in details


async def test_summaries_report_the_published_log_not_a_batch_in_progress(tmp_path: Path) -> None:
    async with Runner(
        RunnerConfig(state_dir=tmp_path, launch=launches.SANDBOX_LAUNCH, harness_environment={})
    ) as runner:
        runner.store.write(
            "test-session",
            SessionRecord.from_spec(runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, model="test-model")),
        )
        session = await runner._load("test-session")
        async with session.journal.batch():
            await session.emit(event_pb2.TurnStarted(turn_id="test-turn"), sources=[])
            (summary,) = runner.summaries()
            assert (summary.last_cursor, summary.active_turn_id) == (0, "")
        (summary,) = runner.summaries()
        assert (summary.last_cursor, summary.active_turn_id) == (1, "test-turn")


async def test_vm_workspace_root_rejects_outside_paths_and_symlink_escapes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    async with Runner(
        RunnerConfig(state_dir=tmp_path / "state", launch=launches.SANDBOX_LAUNCH, workspace_root=workspace)
    ) as runner:
        runner._validate_workspace(str(workspace / "new-session"))
        with pytest.raises(ValueError, match="must be a child"):
            runner._validate_workspace(str(outside / "session"))
        with pytest.raises(ValueError, match="must be a child"):
            runner._validate_workspace(str(workspace / "escape" / "session"))


if __name__ == "__main__":
    pytest_bazel.main()

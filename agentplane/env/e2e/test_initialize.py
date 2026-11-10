"""The Sandbox bootstrap runs once, before the Sandbox's first Session."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest_bazel

from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.driver import RunnerDriver, running
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.proto import session_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.


def bootstrapped(config: RunnerTestConfig, script: str) -> RunnerTestConfig:
    return replace(config, launch=replace(config.launch, bootstrap=script))


async def test_bootstrap_runs_before_the_first_harness_and_not_again(
    config: RunnerTestConfig, gateway: Gateway, history_service: GoHistoryService, spec: session_pb2.SessionSpec
) -> None:
    script = "mkdir -p workspaces\nprintf 'ready\\n' | tee -a workspaces/test-ready\n"
    async with (
        running(bootstrapped(config, script), gateway, history_service),
        RunnerDriver(gateway, history_service.reader) as driver,
    ):
        first = await driver.attach("bootstrap-1", spec=spec)
        second = await driver.attach("bootstrap-2", spec=spec)

    assert first.attached.harness_state == second.attached.harness_state == session_pb2.HARNESS_STATE_STOPPED
    assert (config.state_dir / "workspaces/test-ready").read_text() == "ready\n"


async def test_open_acknowledges_while_runner_bootstrap_is_still_held(
    config: RunnerTestConfig, gateway: Gateway, history_service: GoHistoryService, spec: session_pb2.SessionSpec
) -> None:
    # The script blocks writing to this pipe until the test reads it, so its first step is observable.
    config.state_dir.mkdir()
    os.mkfifo(config.state_dir / "started")
    script = "printf 'first\\n' > started\nwhile [ ! -f continue ]; do sleep 0.01; done\nprintf 'second\\n' >&2\nprintf 'done\\n' > completed\n"
    async with (
        running(bootstrapped(config, script), gateway, history_service),
        RunnerDriver(gateway, history_service.reader) as driver,
    ):
        assert await asyncio.to_thread((config.state_dir / "started").read_text) == "first\n"
        assert await driver.list_sessions() == []
        opening = asyncio.create_task(driver.attach("early-1", spec=spec))
        opened = await opening
        assert opened.attached.harness_state == session_pb2.HARNESS_STATE_STOPPED
        (config.state_dir / "continue").touch()
        completed = config.state_dir / "completed"
        async with asyncio.timeout(10):
            while not completed.exists():  # noqa: ASYNC110  # This marker is written by the subprocess.
                await asyncio.sleep(0.01)

    assert (config.state_dir / "completed").read_text() == "done\n"


async def test_an_initialized_sandbox_records_a_bootstrap_conflict_after_open(
    config: RunnerTestConfig, gateway: Gateway, history_service: GoHistoryService, spec: session_pb2.SessionSpec
) -> None:
    script = "printf 'first\\n' | tee -a initialized\n"
    async with RunnerDriver(gateway, history_service.reader) as driver:
        async with running(bootstrapped(config, script), gateway, history_service):
            await driver.attach("restart-1", spec=spec)

        async with running(bootstrapped(config, script), gateway, history_service):
            await driver.attach("restart-2", spec=spec)

        async with running(bootstrapped(config, "printf 'changed\\n' >> initialized\n"), gateway, history_service):
            changed = await driver.attach("restart-3", spec=spec)
            await changed.submit_input("restart-3-input", "The bootstrap must fail before this runs.")
            await changed.until(lambda entry: entry.event.HasField("harness_launch_failed"))

    assert changed.attached.harness_state == session_pb2.HARNESS_STATE_STOPPED
    assert (config.state_dir / "initialized").read_text() == "first\n"


async def test_a_failed_bootstrap_settles_admitted_commands_and_never_reruns(
    config: RunnerTestConfig, gateway: Gateway, history_service: GoHistoryService, spec: session_pb2.SessionSpec
) -> None:
    script = "echo run >> runs\necho broken >&2\nexit 7\n"
    async with (
        running(bootstrapped(config, script), gateway, history_service),
        RunnerDriver(gateway, history_service.reader) as driver,
    ):
        first = await driver.attach("failed-1", spec=spec)
        await first.submit_input("failed-1-input", "Must not reach the harness.")
        await first.until(lambda entry: entry.event.HasField("harness_launch_failed"))
        second = await driver.attach("failed-2", spec=spec)
        await second.submit_input("failed-2-input", "Must not reach the harness.")
        await second.until(lambda entry: entry.event.HasField("command_failed"))
        sessions = await driver.list_sessions()

    assert len(sessions) == 2
    assert all(session.harness_state == session_pb2.HARNESS_STATE_STOPPED for session in sessions)
    assert (config.state_dir / "runs").read_text() == "run\n"


if __name__ == "__main__":
    pytest_bazel.main()

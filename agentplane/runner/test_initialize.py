"""The Sandbox bootstrap runs once, before the Sandbox's first Session, and is durable and replayable."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import grpc
import pytest_bazel

from agentplane.proto import runner_pb2
from agentplane.proto.runner_control_pb2 import RunnerFacts
from agentplane.runner.config import RunnerConfig
from agentplane.runner.initialization import InitializationLog
from agentplane.runner.testing.driver import RunnerDriver, refusal, running
from agentplane.runner.testing.gateway import Gateway

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


def bootstrapped(config: RunnerConfig, script: str) -> RunnerConfig:
    return replace(config, launch=replace(config.launch, bootstrap=script))


def recorded(state: Path) -> list[runner_pb2.InitializationEvent]:
    log = InitializationLog(state / "initialization" / "events.jsonl")
    try:
        return list(log.since(0))
    finally:
        log.close()


async def test_bootstrap_runs_before_the_first_session_and_not_again(
    config: RunnerConfig, gateway: Gateway, spec: runner_pb2.SessionSpec
) -> None:
    script = "mkdir -p workspaces\nprintf 'ready\\n' | tee -a workspaces/public-coder-ready\n"
    async with running(bootstrapped(config, script), gateway):
        driver = RunnerDriver(gateway)
        first = await driver.attach("bootstrap-1", spec=spec)
        second = await driver.attach("bootstrap-2", spec=spec)

    assert first.attached.harness_state == second.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    assert (config.state_dir / "workspaces/public-coder-ready").read_text() == "ready\n"
    output, result = recorded(config.state_dir)
    assert output.output == runner_pb2.InitializationOutput(
        stream=runner_pb2.INITIALIZATION_STREAM_STDOUT, data=b"ready\n"
    )
    assert (result.result.executed, result.result.exit_code) == (True, 0)


async def test_bootstrap_runs_at_runner_start_before_any_session_opens(
    config: RunnerConfig, gateway: Gateway, spec: runner_pb2.SessionSpec
) -> None:
    # The script blocks writing to this pipe until the test reads it, so its first step is observable.
    config.state_dir.mkdir()
    os.mkfifo(config.state_dir / "started")
    script = "printf 'first\\n' > started\nwhile [ ! -f continue ]; do sleep 0.01; done\nprintf 'second\\n' >&2\n"
    async with running(bootstrapped(config, script), gateway):
        assert await asyncio.to_thread((config.state_dir / "started").read_text) == "first\n"
        driver = RunnerDriver(gateway)
        assert await driver.list_sessions() == []
        opening = asyncio.create_task(driver.attach("early-1", spec=spec))
        (config.state_dir / "continue").touch()
        opened = await opening

    assert opened.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    (output, finished) = recorded(config.state_dir)
    assert output.output == runner_pb2.InitializationOutput(
        stream=runner_pb2.INITIALIZATION_STREAM_STDERR, data=b"second\n"
    )
    assert finished.result.exit_code == 0


async def test_the_gateway_is_told_how_the_bootstrap_stands(config: RunnerConfig, gateway: Gateway) -> None:
    script = "while [ ! -f continue ]; do sleep 0.01; done\n"
    async with running(bootstrapped(config, script), gateway):
        await gateway.control.wait_bootstrap(RunnerFacts.BOOTSTRAP_RUNNING)
        (config.state_dir / "continue").touch()
        await gateway.control.wait_bootstrap(RunnerFacts.BOOTSTRAP_SUCCEEDED)


async def test_an_initialized_sandbox_refuses_a_different_script_after_restart(
    config: RunnerConfig, gateway: Gateway, spec: runner_pb2.SessionSpec
) -> None:
    script = "printf 'first\\n' | tee -a initialized\n"
    async with running(bootstrapped(config, script), gateway):
        await RunnerDriver(gateway).attach("restart-1", spec=spec)

    async with running(bootstrapped(config, script), gateway):
        await RunnerDriver(gateway).attach("restart-2", spec=spec)

    async with running(bootstrapped(config, "printf 'changed\\n' >> initialized\n"), gateway):
        changed = await refusal(RunnerDriver(gateway).attach("restart-3", spec=spec))

    assert changed == grpc.StatusCode.ALREADY_EXISTS
    assert (config.state_dir / "initialized").read_text() == "first\n"


async def test_a_failed_bootstrap_refuses_every_session_and_never_reruns(
    config: RunnerConfig, gateway: Gateway, spec: runner_pb2.SessionSpec
) -> None:
    script = "echo run >> runs\necho broken >&2\nexit 7\n"
    async with running(bootstrapped(config, script), gateway):
        driver = RunnerDriver(gateway)
        await gateway.control.wait_bootstrap(RunnerFacts.BOOTSTRAP_FAILED)
        first = await refusal(driver.attach("failed-1", spec=spec))
        repeated = await refusal(driver.attach("failed-2", spec=spec))
        sessions = await driver.list_sessions()

    assert first == repeated == grpc.StatusCode.FAILED_PRECONDITION
    assert sessions == []
    assert recorded(config.state_dir)[0].output.data == b"broken\n"
    assert (config.state_dir / "runs").read_text() == "run\n"


if __name__ == "__main__":
    pytest_bazel.main()

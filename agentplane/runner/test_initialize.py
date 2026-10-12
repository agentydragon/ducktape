"""Sandbox initialization is single-choice, runs at most once, and is durable and replayable."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import grpc
import pytest_bazel

from agentplane.proto import runner_pb2
from agentplane.runner.config import RunnerConfig
from agentplane.runner.initialization import InitializationLog
from agentplane.runner.testing.driver import RunnerDriver, refusal, running
from agentplane.runner.testing.gateway import Gateway

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


def recorded(state: Path) -> list[runner_pb2.InitializationEvent]:
    log = InitializationLog(state / "initialization" / "events.jsonl")
    try:
        return list(log.since(0))
    finally:
        log.close()


async def test_initialize_executes_once_and_replays_its_outcome(tmp_path: Path, gateway: Gateway) -> None:
    state = tmp_path / "state"
    state.mkdir()
    driver = RunnerDriver(gateway)
    async with running(RunnerConfig(state_dir=state), gateway):
        script = "mkdir -p workspaces\nprintf 'ready\\n' | tee -a workspaces/public-coder-ready\n"
        first = await driver.initialize(script)
        repeated = await driver.initialize(script)

    assert (first.sequence, first.attempt) == (2, 1)
    assert (first.result.executed, first.result.exit_code) == (True, 0)
    assert repeated == first
    assert recorded(state)[0].output == runner_pb2.InitializationOutput(
        stream=runner_pb2.INITIALIZATION_STREAM_STDOUT, data=b"ready\n"
    )
    assert (state / "workspaces/public-coder-ready").read_text() == "ready\n"


async def test_an_abandoned_request_leaves_the_bootstrap_running(tmp_path: Path, gateway: Gateway) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # The script blocks writing to this pipe until the test reads it, so its first step is observable.
    os.mkfifo(state / "started")
    driver = RunnerDriver(gateway)
    async with running(RunnerConfig(state_dir=state), gateway):
        script = "printf 'first\\n' > started\nwhile [ ! -f continue ]; do sleep 0.01; done\nprintf 'second\\n' >&2\n"
        abandoned = asyncio.create_task(driver.initialize(script))
        assert await asyncio.to_thread((state / "started").read_text) == "first\n"
        abandoned.cancel()
        (state / "continue").touch()
        result = await driver.initialize(script)

    assert result.result.exit_code == 0
    (output, finished) = recorded(state)
    assert output.output == runner_pb2.InitializationOutput(
        stream=runner_pb2.INITIALIZATION_STREAM_STDERR, data=b"second\n"
    )
    assert finished == result


async def test_initialized_sandbox_refuses_a_different_script_after_restart(tmp_path: Path, gateway: Gateway) -> None:
    state = tmp_path / "state"
    state.mkdir()
    script = "printf 'first\\n' | tee -a initialized\n"
    driver = RunnerDriver(gateway)
    async with running(RunnerConfig(state_dir=state), gateway):
        first = await driver.initialize(script)

    async with running(RunnerConfig(state_dir=state), gateway):
        replayed = await driver.initialize(script)
        changed = await refusal(driver.initialize("printf 'changed\\n' >> initialized\n"))

    assert replayed == first
    assert replayed.result.exit_code == 0
    assert changed == grpc.StatusCode.ALREADY_EXISTS
    assert (state / "initialized").read_text() == "first\n"


async def test_failed_initialize_is_replayed_and_never_rerun(tmp_path: Path, gateway: Gateway) -> None:
    state = tmp_path / "state"
    state.mkdir()
    driver = RunnerDriver(gateway)
    async with running(RunnerConfig(state_dir=state), gateway):
        script = "echo run >> runs\necho broken >&2\nexit 7\n"
        first = await driver.initialize(script)
        repeated = await driver.initialize(script)

    assert (first.result.executed, first.result.exit_code) == (True, 7)
    assert recorded(state)[0].output.data == b"broken\n"
    assert repeated == first
    assert (state / "runs").read_text() == "run\n"


if __name__ == "__main__":
    pytest_bazel.main()

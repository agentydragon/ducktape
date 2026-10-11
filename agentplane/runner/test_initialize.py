"""Sandbox initialization is single-choice, runs at most once, and is durable, streamed and replayable."""

from __future__ import annotations

from pathlib import Path

import grpc
import pytest
import pytest_bazel

from agentplane.proto import runner_pb2
from agentplane.runner.config import RunnerConfig
from agentplane.runner.service import serve
from agentplane.runner.testing.fixtures import RunnerClientFactory

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def collect(call: grpc.aio.UnaryStreamCall) -> list[runner_pb2.InitializationEvent]:
    return [event async for event in call]


async def test_initialize_executes_once_and_replays_its_output(
    tmp_path: Path, runner_client_factory: RunnerClientFactory
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    server, runner, port = await serve(RunnerConfig(state_dir=state))
    script = "mkdir -p workspaces\nprintf 'ready\\n' | tee -a workspaces/public-coder-ready\n"
    try:
        async with runner_client_factory(f"127.0.0.1:{port}") as client:
            first = await collect(client.initialize_events(script))
            repeated = await collect(client.initialize_events(script))
    finally:
        await runner.stop()
        await server.stop(0)

    assert [(event.sequence, event.attempt) for event in first] == [(1, 1), (2, 1)]
    assert first[0].output == runner_pb2.InitializationOutput(
        stream=runner_pb2.INITIALIZATION_STREAM_STDOUT, data=b"ready\n"
    )
    assert (first[-1].result.executed, first[-1].result.exit_code) == (True, 0)
    assert repeated == first
    assert (state / "workspaces/public-coder-ready").read_text() == "ready\n"


async def test_initialize_reconnect_replays_after_the_client_cursor(
    tmp_path: Path, runner_client_factory: RunnerClientFactory
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    server, runner, port = await serve(RunnerConfig(state_dir=state))
    script = "printf 'first\\n'\nwhile [ ! -f continue ]; do sleep 0.01; done\nprintf 'second\\n' >&2\n"
    try:
        async with runner_client_factory(f"127.0.0.1:{port}") as client:
            disconnected = client.initialize_events(script)
            first = await disconnected.read()
            assert isinstance(first, runner_pb2.InitializationEvent)
            disconnected.cancel()
            (state / "continue").touch()
            resumed = await collect(client.initialize_events(script, after_sequence=first.sequence))
    finally:
        await runner.stop()
        await server.stop(0)

    assert first.output.data == b"first\n"
    assert [event.sequence for event in resumed] == [2, 3]
    assert resumed[0].output == runner_pb2.InitializationOutput(
        stream=runner_pb2.INITIALIZATION_STREAM_STDERR, data=b"second\n"
    )
    assert resumed[1].result.exit_code == 0


async def test_initialized_sandbox_refuses_a_different_script_after_restart(
    tmp_path: Path, runner_client_factory: RunnerClientFactory
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    script = "printf 'first\\n' | tee -a initialized\n"
    first_server, first_runner, first_port = await serve(RunnerConfig(state_dir=state))
    try:
        async with runner_client_factory(f"127.0.0.1:{first_port}") as first_client:
            await first_client.initialize(script)
    finally:
        await first_runner.stop()
        await first_server.stop(0)

    server, runner, port = await serve(RunnerConfig(state_dir=state))
    try:
        async with runner_client_factory(f"127.0.0.1:{port}") as client:
            replayed = await collect(client.initialize_events(script))
            with pytest.raises(grpc.aio.AioRpcError) as changed_script:
                await client.initialize("printf 'changed\\n' >> initialized\n")
    finally:
        await runner.stop()
        await server.stop(0)

    assert replayed[0].output.data == b"first\n"
    assert replayed[-1].result.exit_code == 0
    assert changed_script.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "different bootstrap script" in (changed_script.value.details() or "")
    assert (state / "initialized").read_text() == "first\n"


async def test_failed_initialize_is_replayed_and_never_rerun(
    tmp_path: Path, runner_client_factory: RunnerClientFactory
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    server, runner, port = await serve(RunnerConfig(state_dir=state))
    script = "echo run >> runs\necho broken >&2\nexit 7\n"
    try:
        async with runner_client_factory(f"127.0.0.1:{port}") as client:
            first = await collect(client.initialize_events(script))
            repeated = await collect(client.initialize_events(script))
    finally:
        await runner.stop()
        await server.stop(0)

    assert (first[-1].result.executed, first[-1].result.exit_code) == (True, 7)
    assert first[0].output.data == b"broken\n"
    assert repeated == first
    assert (state / "runs").read_text() == "run\n"


if __name__ == "__main__":
    pytest_bazel.main()

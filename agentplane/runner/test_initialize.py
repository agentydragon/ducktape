"""Sandbox initialization is single-choice, durable, streamed, and replayable."""

from __future__ import annotations

from pathlib import Path

import grpc
import pytest
import pytest_bazel

from agentplane.runner import protocol_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.config import RunnerConfig
from agentplane.runner.service import serve

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def collect(call: grpc.aio.UnaryStreamCall) -> list[protocol_pb2.InitializationEvent]:
    return [event async for event in call]


async def test_initialize_executes_once_and_replays_its_output(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    server, runner, port = await serve(RunnerConfig(state_dir=state))
    client = RunnerClient(f"127.0.0.1:{port}")
    script = "mkdir -p workspaces\nprintf 'ready\\n' | tee -a workspaces/public-coder-ready\n"
    try:
        first = await collect(client.initialize_events(script))
        repeated = await collect(client.initialize_events(script))
    finally:
        await client.close()
        await runner.stop()
        await server.stop(0)

    assert [(event.sequence, event.attempt) for event in first] == [(1, 1), (2, 1)]
    assert first[0].output == protocol_pb2.InitializationOutput(
        stream=protocol_pb2.INITIALIZATION_STREAM_STDOUT, data=b"ready\n"
    )
    assert (first[-1].result.executed, first[-1].result.exit_code) == (True, 0)
    assert repeated == first
    assert (state / "workspaces/public-coder-ready").read_text() == "ready\n"


async def test_initialize_reconnect_replays_after_the_client_cursor(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    server, runner, port = await serve(RunnerConfig(state_dir=state))
    client = RunnerClient(f"127.0.0.1:{port}")
    script = "printf 'first\\n'\nwhile [ ! -f continue ]; do sleep 0.01; done\nprintf 'second\\n' >&2\n"
    try:
        disconnected = client.initialize_events(script)
        first = await disconnected.read()
        assert isinstance(first, protocol_pb2.InitializationEvent)
        disconnected.cancel()
        (state / "continue").touch()
        resumed = await collect(client.initialize_events(script, after_sequence=first.sequence))
    finally:
        await client.close()
        await runner.stop()
        await server.stop(0)

    assert first.output.data == b"first\n"
    assert [event.sequence for event in resumed] == [2, 3]
    assert resumed[0].output == protocol_pb2.InitializationOutput(
        stream=protocol_pb2.INITIALIZATION_STREAM_STDERR, data=b"second\n"
    )
    assert resumed[1].result.exit_code == 0


async def test_initialized_sandbox_refuses_a_different_script_after_restart(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    script = "printf 'first\\n' | tee -a initialized\n"
    first_server, first_runner, first_port = await serve(RunnerConfig(state_dir=state))
    first_client = RunnerClient(f"127.0.0.1:{first_port}")
    try:
        await first_client.initialize(script)
    finally:
        await first_client.close()
        await first_runner.stop()
        await first_server.stop(0)

    server, runner, port = await serve(RunnerConfig(state_dir=state))
    client = RunnerClient(f"127.0.0.1:{port}")
    try:
        replayed = await collect(client.initialize_events(script))
        with pytest.raises(grpc.aio.AioRpcError) as changed_script:
            await client.initialize("printf 'changed\\n' >> initialized\n")
    finally:
        await client.close()
        await runner.stop()
        await server.stop(0)

    assert replayed[0].output.data == b"first\n"
    assert replayed[-1].result.exit_code == 0
    assert changed_script.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "different bootstrap script" in (changed_script.value.details() or "")
    assert (state / "initialized").read_text() == "first\n"


async def test_failed_initialize_output_is_saved_and_the_same_script_may_be_retried(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    server, runner, port = await serve(RunnerConfig(state_dir=state))
    client = RunnerClient(f"127.0.0.1:{port}")
    script = "echo broken >&2\nexit 7\n"
    try:
        first = await collect(client.initialize_events(script))
        retried = await collect(client.initialize_events(script))
    finally:
        await client.close()
        await runner.stop()
        await server.stop(0)

    assert (first[-1].result.executed, first[-1].result.exit_code) == (True, 7)
    assert (retried[-1].result.executed, retried[-1].result.exit_code) == (True, 7)
    assert [event.attempt for event in retried if event.HasField("result")] == [1, 2]
    assert (
        b"".join(
            event.output.data
            for event in retried
            if event.HasField("output") and event.output.stream == protocol_pb2.INITIALIZATION_STREAM_STDERR
        )
        == b"broken\nbroken\n"
    )


if __name__ == "__main__":
    pytest_bazel.main()

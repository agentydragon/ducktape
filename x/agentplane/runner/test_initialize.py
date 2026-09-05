"""The runner initialization RPC executes configured source once per stable app identity."""

from __future__ import annotations

import hashlib
from pathlib import Path

import grpc
import pytest
import pytest_bazel

from x.agentplane.runner.client import RunnerClient
from x.agentplane.runner.config import RunnerConfig
from x.agentplane.runner.service import serve


async def test_initialize_executes_before_marking_the_identity_complete(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    server, runner, port = await serve(RunnerConfig(state_dir=state))
    client = RunnerClient(f"127.0.0.1:{port}")
    script = "mkdir -p workspaces\nprintf 'ready\\n' >> workspaces/public-coder-ready\n"
    key = hashlib.sha256(b"sandbox-preset:public-coder").hexdigest()
    try:
        first = await client.initialize(key, script)
        repeated = await client.initialize(key, script)
    finally:
        await client.close()
        await runner.stop()
        await server.stop(0)

    assert (first.executed, first.exit_code, first.stderr) == (True, 0, "")
    assert (repeated.executed, repeated.exit_code) == (False, 0)
    assert (state / "workspaces/public-coder-ready").read_text() == "ready\n"
    assert (state / "initializations" / key).read_text() == f"{hashlib.sha256(script.encode()).hexdigest()}\n"


async def test_initialized_sandbox_refuses_a_different_identity_or_script_after_restart(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    script = "printf 'first\\n' >> initialized\n"
    key = hashlib.sha256(b"sandbox-preset:first").hexdigest()
    other_key = hashlib.sha256(b"sandbox-preset:second").hexdigest()

    first_server, first_runner, first_port = await serve(RunnerConfig(state_dir=state))
    first_client = RunnerClient(f"127.0.0.1:{first_port}")
    try:
        await first_client.initialize(key, script)
    finally:
        await first_client.close()
        await first_runner.stop()
        await first_server.stop(0)

    server, runner, port = await serve(RunnerConfig(state_dir=state))
    client = RunnerClient(f"127.0.0.1:{port}")
    try:
        repeated = await client.initialize(key, script)
        with pytest.raises(grpc.aio.AioRpcError) as changed_script:
            await client.initialize(key, "printf 'changed\\n' >> initialized\n")
        with pytest.raises(grpc.aio.AioRpcError) as changed_identity:
            await client.initialize(other_key, "printf 'second\\n' >> initialized\n")
    finally:
        await client.close()
        await runner.stop()
        await server.stop(0)

    assert (repeated.executed, repeated.exit_code) == (False, 0)
    assert changed_script.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "script differs" in (changed_script.value.details() or "")
    assert changed_identity.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "different bootstrap" in (changed_identity.value.details() or "")
    assert (state / "initialized").read_text() == "first\n"


async def test_failed_initialize_is_visible_and_may_be_retried(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    server, runner, port = await serve(RunnerConfig(state_dir=state))
    client = RunnerClient(f"127.0.0.1:{port}")
    script = "echo broken >&2\nexit 7\n"
    key = hashlib.sha256(script.encode()).hexdigest()
    try:
        first = await client.initialize(key, script)
        retried = await client.initialize(key, script)
    finally:
        await client.close()
        await runner.stop()
        await server.stop(0)

    assert (first.executed, first.exit_code, first.stderr) == (True, 7, "broken\n")
    assert (retried.executed, retried.exit_code) == (True, 7)
    assert not (state / "initializations" / key).exists()


if __name__ == "__main__":
    pytest_bazel.main()

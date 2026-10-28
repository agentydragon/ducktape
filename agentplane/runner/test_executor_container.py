"""Exercise Codex shell dispatch and recovery through a private companion container."""

from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.testing import events, launches
from agentplane.runner.testing.config import RunnerTestConfig
from agentplane.runner.testing.driver import RunnerDriver, SessionFollow, SessionSealedError
from agentplane.runner.testing.executor_companion import (
    ExecutorCompanion,
    ExecutorStatus,
    executor_health_ok,
    inspect_executor,
)
from agentplane.runner.testing.fixtures import ModelEndpoint
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner.testing.process_fixtures import wait_for_exit
from agentplane.testing.scripted_model import ScriptedModel, ShellCall, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CODEX], ids=["codex"])


@pytest.fixture
def config(
    harness: runner_pb2.Harness, endpoint: ModelEndpoint, tmp_path: Path, executor_companion: ExecutorCompanion
) -> RunnerTestConfig:
    runner_config = launches.config(
        harness,
        endpoint.origin,
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        codex_executor_url=executor_companion.url,
        codex_executor_socket=executor_companion.socket,
    )
    environment = {**runner_config.harness_environment, "PATH": "/usr/bin:/bin"}
    return replace(runner_config, harness_environment=environment)


async def _wait_for_generation(path: Path, previous: str, *, owned: bool) -> ExecutorStatus:
    async with asyncio.timeout(30):
        while True:
            try:
                status = await asyncio.to_thread(inspect_executor, path)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            if status.generation != previous and status.owned is owned:
                return status
            await asyncio.sleep(0.1)


async def _wait_for_owned(path: Path, generation: str) -> ExecutorStatus:
    async with asyncio.timeout(30):
        while True:
            try:
                status = await asyncio.to_thread(inspect_executor, path)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            if status.generation == generation and status.owned:
                return status
            await asyncio.sleep(0.1)


def _host_pid_for_marker(companion: ExecutorCompanion, marker: str) -> int | None:
    process_table = companion.container.get_wrapped_container().top()
    pid_column = process_table["Titles"].index("PID")
    command_column = process_table["Titles"].index("CMD")
    matches = [row for row in process_table["Processes"] if marker in row[command_column]]
    if len(matches) > 1:
        raise AssertionError(f"expected one process marked {marker!r}, found {matches}")
    if not matches:
        return None

    pid = int(matches[0][pid_column])
    try:
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError as error:
        raise AssertionError(f"Docker top PID {pid} is not visible in the test process namespace") from error
    if marker.encode() not in cmdline:
        raise AssertionError(f"Docker top PID {pid} does not have expected marker {marker!r}: {cmdline!r}")
    return pid


def _wait_for_host_pid(companion: ExecutorCompanion, marker: str) -> int:
    deadline = time.monotonic() + 30
    while (pid := _host_pid_for_marker(companion, marker)) is None:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"process marked {marker!r} did not appear in Docker top")
        time.sleep(0.1)
    return pid


def _kill_exec_server(companion: ExecutorCompanion) -> None:
    script = """
    for process in /proc/[0-9]*; do
      args=$(tr '\\000' ' ' < "$process/cmdline" 2>/dev/null) || continue
      case "$args" in
        "/opt/codex/bin/codex exec-server --listen "*)
          kill -KILL "${process##*/}"
          exit 0
          ;;
      esac
    done
    exit 1
    """
    result = companion.container.get_wrapped_container().exec_run(["/bin/sh", "-c", script])
    if result.exit_code:
        raise RuntimeError(f"could not kill Codex exec-server: {result.output!r}")


def _wait_for_container_exit(companion: ExecutorCompanion) -> None:
    companion.container.get_wrapped_container().wait(timeout=30)


def _start_container(companion: ExecutorCompanion) -> None:
    # Starting the stopped container models kubelet's restart after PID 1 exits.
    companion.container.get_wrapped_container().start()


def _detached_command(marker: str) -> str:
    script = "tail -f /dev/null & wait"
    return f"setsid sh -c {shlex.quote(script)} {shlex.quote(marker)} </dev/null >/dev/null 2>&1 &"


async def _codex_process(driver: RunnerDriver) -> runner_pb2.HarnessProcessStatus:
    runner = await driver.gateway.control.runner()
    status: runner_pb2.RunnerStatus = await runner.GetStatus(runner_pb2.GetStatusRequest())
    (process,) = status.processes
    assert process.harness == runner_pb2.HARNESS_CODEX
    return process


async def _reply_to_tool(
    model: ScriptedModel, session: SessionFollow, call_id: str, command: str, result_text: str, expected_output: str
) -> None:
    request = await model.request()
    await model.reply(request, ShellCall(call_id, command))
    result = await model.request()
    outputs = [output for output in result.tool_outputs if output.call_id == call_id]
    assert len(outputs) == 1, f"expected one output for {call_id!r}, got {result.tool_outputs!r}"
    (output,) = outputs
    assert output.contains(expected_output)
    await model.reply(result, Text(result_text))
    done = await session.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED


async def test_companion_namespace_restarts_without_restarting_codex_app_server(
    runner: RunningRunner,
    driver: RunnerDriver,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    executor_companion: ExecutorCompanion,
) -> None:
    assert executor_health_ok(executor_companion.health_url), "idle companion should pass its native health endpoint"
    session = await driver.attach("isolated-executor", spec=spec)
    detached_marker = f"detached-child-{uuid4().hex}"
    first_command = f"{_detached_command(detached_marker)} printf FIRST_TOOL_OK"

    await session.submit_input("input-1", "Run the first shell tool and report its output.")
    await _reply_to_tool(model, session, "call-first", first_command, "FIRST_DONE", "FIRST_TOOL_OK")
    detached_host_pid = await asyncio.to_thread(_wait_for_host_pid, executor_companion, detached_marker)
    initial_executor = await asyncio.to_thread(inspect_executor, executor_companion.socket)
    assert initial_executor.owned
    assert executor_health_ok(executor_companion.health_url), "claimed companion should remain healthy"
    initial_app_server = await _codex_process(driver)
    assert initial_app_server.HasField("pid")

    await asyncio.to_thread(_kill_exec_server, executor_companion)
    await asyncio.to_thread(_wait_for_container_exit, executor_companion)
    assert not executor_health_ok(executor_companion.health_url), "stopped companion must close its health listener"
    await asyncio.wait_for(wait_for_exit(detached_host_pid), timeout=30)
    await asyncio.to_thread(_start_container, executor_companion)
    restarted_executor = await _wait_for_generation(executor_companion.socket, initial_executor.generation, owned=True)
    assert executor_health_ok(executor_companion.health_url), "restarted companion should reopen its health listener"

    recovered_app_server = await _codex_process(driver)
    assert recovered_app_server.pid == initial_app_server.pid

    second_detached_marker = f"detached-child-{uuid4().hex}"
    await session.submit_input("input-2", "Run a fresh shell tool and report its output.")
    await _reply_to_tool(
        model,
        session,
        "call-second",
        f"{_detached_command(second_detached_marker)} printf SECOND_TOOL_OK",
        "SECOND_DONE",
        "SECOND_TOOL_OK",
    )
    second_detached_host_pid = await asyncio.to_thread(_wait_for_host_pid, executor_companion, second_detached_marker)
    assert restarted_executor.generation != initial_executor.generation
    assert (await _codex_process(driver)).pid == initial_app_server.pid

    await runner.crash()
    await asyncio.to_thread(_wait_for_container_exit, executor_companion)
    await asyncio.wait_for(wait_for_exit(second_detached_host_pid), timeout=30)
    await asyncio.to_thread(_start_container, executor_companion)
    idle_executor = await _wait_for_generation(executor_companion.socket, restarted_executor.generation, owned=False)
    await runner.start()

    session = await driver.attach("isolated-executor", spec=spec, after_cursor=session.cursor)
    await session.submit_input("input-3", "Run a fresh shell tool and report its output.")
    third_detached_marker = f"detached-child-{uuid4().hex}"
    await _reply_to_tool(
        model,
        session,
        "call-third",
        f"{_detached_command(third_detached_marker)} printf THIRD_TOOL_OK",
        "THIRD_DONE",
        "THIRD_TOOL_OK",
    )
    owned_executor = await _wait_for_owned(executor_companion.socket, idle_executor.generation)
    assert owned_executor.generation == idle_executor.generation

    third_detached_host_pid = await asyncio.to_thread(_wait_for_host_pid, executor_companion, third_detached_marker)
    app_server = await _codex_process(driver)
    assert app_server.HasField("pid")
    quiescing = asyncio.create_task(driver.quiesce())
    await asyncio.to_thread(_wait_for_container_exit, executor_companion)
    await asyncio.wait_for(wait_for_exit(third_detached_host_pid), timeout=30)
    await asyncio.wait_for(wait_for_exit(app_server.pid), timeout=30)
    assert not quiescing.done()
    await asyncio.to_thread(_start_container, executor_companion)
    await _wait_for_generation(executor_companion.socket, owned_executor.generation, owned=False)
    await quiescing
    with pytest.raises(SessionSealedError) as sealed:
        await session.until(lambda _: False)
    assert sealed.value.cursor == session.cursor


if __name__ == "__main__":
    pytest_bazel.main()

"""Exercise Codex shell dispatch and recovery through a private companion container."""

from __future__ import annotations

import asyncio
import os
import shlex
import textwrap
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import grpc.aio
import pytest
import pytest_bazel
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, Retrying, retry, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.e2e.testing import events, launches
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.driver import RunnerDriver, SessionFollow, SessionSealedError
from agentplane.env.e2e.testing.fixtures import ModelEndpoint
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.env.e2e.testing.process_fixtures import wait_for_exit
from agentplane.env.runtime.execution import executor_pb2, executor_pb2_grpc
from agentplane.env.runtime.execution.companion.control import control_pb2, control_pb2_grpc
from agentplane.env.runtime.execution.testing.codex_exec_server_container import (
    CodexExecServerContainer,
    CodexExecServerContainerStatus,
    inspect_codex_exec_server_container,
)
from agentplane.proto import event_pb2, identity_pb2, session_pb2
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.scripted_model import ScriptedModel, ShellCall, Text
from devinfra.oci.oci import load_oci_image
from third_party.containers import debian_slim
from util.testing.container_logs import LoggedContainer

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/env/runtime/execution/testing:codex_exec_server_container
pytest_plugins = ("agentplane.env.runtime.execution.testing.codex_exec_server_container",)

pytestmark = pytest.mark.parametrize("harness", [session_pb2.HARNESS_CODEX], ids=["codex"])


@pytest.fixture
def config(
    harness: session_pb2.Harness,
    endpoint: ModelEndpoint,
    tmp_path: Path,
    codex_exec_server_container: CodexExecServerContainer,
    sandbox: SandboxCluster,
) -> RunnerTestConfig:
    runner_config = launches.config(
        harness,
        endpoint.origin,
        sandbox=sandbox.identity,
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        codex_executor_url=codex_exec_server_container.url,
        codex_executor_socket=codex_exec_server_container.socket,
    )
    environment = {**runner_config.harness_environment, "PATH": "/usr/bin:/bin"}
    return replace(runner_config, harness_environment=environment)


async def _wait_for_status(
    path: Path, accept: Callable[[CodexExecServerContainerStatus], bool]
) -> CodexExecServerContainerStatus:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30),
        wait=wait_fixed(0.1),
        retry=retry_if_exception_type((grpc.RpcError, AssertionError)),
        reraise=True,
    ):
        with attempt:
            status = await asyncio.to_thread(inspect_codex_exec_server_container, path)
            assert accept(status), f"executor companion stayed at {status=}"
    return status


async def _wait_for_generation(path: Path, previous: str, *, owned: bool) -> CodexExecServerContainerStatus:
    return await _wait_for_status(path, lambda status: status.generation != previous and status.owned is owned)


async def _wait_for_owned(path: Path, generation: str) -> CodexExecServerContainerStatus:
    return await _wait_for_status(path, lambda status: status.generation == generation and status.owned)


def _host_pid_for_marker(companion: CodexExecServerContainer, marker: str) -> int | None:
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


@retry(stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(LookupError), reraise=True)
def _wait_for_host_pid(companion: CodexExecServerContainer, marker: str) -> int:
    if (pid := _host_pid_for_marker(companion, marker)) is None:
        raise LookupError(f"process marked {marker!r} did not appear in Docker top")
    return pid


def _kill_exec_server(companion: CodexExecServerContainer) -> None:
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


def _wait_for_executor_file(path: Path) -> None:
    for attempt in Retrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.01), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            assert path.exists(), f"executor did not create {path}"


def _detached_command(marker: str) -> str:
    script = "tail -f /dev/null & wait"
    return f"setsid sh -c {shlex.quote(script)} {shlex.quote(marker)} </dev/null >/dev/null 2>&1 &"


async def _codex_process(driver: RunnerDriver) -> session_pb2.HarnessProcessStatus:
    runner = await driver.gateway.control.runner()
    status: session_pb2.EnvironmentRuntimeStatus = await runner.GetStatus(session_pb2.GetStatusRequest())
    (process,) = status.processes
    assert process.harness == session_pb2.HARNESS_CODEX
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


async def test_codex_exec_server_container_recovers_without_restarting_app_server(
    runner: RunningRunner,
    driver: RunnerDriver,
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
    codex_exec_server_container: CodexExecServerContainer,
) -> None:
    session = await driver.attach("isolated-executor", spec=spec)
    detached_marker = f"detached-child-{uuid4().hex}"
    first_command = f"{_detached_command(detached_marker)} printf FIRST_TOOL_OK"

    await session.submit_input("input-1", "Run the first shell tool and report its output.")
    await _reply_to_tool(model, session, "call-first", first_command, "FIRST_DONE", "FIRST_TOOL_OK")
    detached_host_pid = await asyncio.to_thread(_wait_for_host_pid, codex_exec_server_container, detached_marker)
    initial_executor = await asyncio.to_thread(inspect_codex_exec_server_container, codex_exec_server_container.socket)
    assert initial_executor.owned
    await asyncio.to_thread(codex_exec_server_container.check_health)
    initial_app_server = await _codex_process(driver)
    assert initial_app_server.HasField("pid")

    await asyncio.to_thread(_kill_exec_server, codex_exec_server_container)
    await asyncio.wait_for(wait_for_exit(detached_host_pid), timeout=30)
    # Runtime can reclaim the fresh idle generation before this poll observes it.
    restarted_executor = await _wait_for_generation(
        codex_exec_server_container.socket, initial_executor.generation, owned=True
    )
    await asyncio.to_thread(codex_exec_server_container.check_health)

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
    second_detached_host_pid = await asyncio.to_thread(
        _wait_for_host_pid, codex_exec_server_container, second_detached_marker
    )
    assert restarted_executor.generation != initial_executor.generation
    assert (await _codex_process(driver)).pid == initial_app_server.pid

    await runner.crash()
    await asyncio.wait_for(wait_for_exit(second_detached_host_pid), timeout=30)
    idle_executor = await _wait_for_generation(
        codex_exec_server_container.socket, restarted_executor.generation, owned=False
    )
    await asyncio.to_thread(codex_exec_server_container.check_health)
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
    owned_executor = await _wait_for_owned(codex_exec_server_container.socket, idle_executor.generation)
    assert owned_executor.generation == idle_executor.generation

    third_detached_host_pid = await asyncio.to_thread(
        _wait_for_host_pid, codex_exec_server_container, third_detached_marker
    )
    app_server = await _codex_process(driver)
    assert app_server.HasField("pid")
    app_server_host_pid = await runner.host_pid(app_server.pid)
    quiescing = asyncio.create_task(driver.quiesce())
    await asyncio.wait_for(wait_for_exit(third_detached_host_pid), timeout=30)
    await asyncio.wait_for(wait_for_exit(app_server_host_pid), timeout=30)
    idle_after_quiesce = await _wait_for_generation(
        codex_exec_server_container.socket, owned_executor.generation, owned=False
    )
    assert idle_after_quiesce.generation != owned_executor.generation
    await asyncio.to_thread(codex_exec_server_container.check_health)
    await quiescing
    with pytest.raises(SessionSealedError) as sealed:
        await session.until(lambda _: False)
    assert sealed.value.cursor == session.cursor


async def test_executor_exit_does_not_join_escaped_output_readers(harness: session_pb2.Harness, tmp_path: Path) -> None:
    debian_image = load_oci_image(debian_slim.LAYOUT_RLOCATION)
    execution_binary = (Runfiles.CreateOrRaise().root() / launches.EXECUTION_BINARY).resolve()
    socket_path = tmp_path / "executor.sock"
    ready_path = tmp_path / "server-ready"
    exit_path = tmp_path / "server-exit"
    pid_path = tmp_path / "pipe-holder.pid"
    marker = f"executor-pipe-holder-{uuid4().hex}"
    (tmp_path / "exec-server").write_text(
        textwrap.dedent(
            f"""\
            setsid /bin/sh -c \\
              'printf "%s\\n" "$$" > "$1"; trap "" TERM; while :; do sleep 1; done' \\
              {shlex.quote(marker)} {shlex.quote(str(pid_path))} &
            while [ ! -s {shlex.quote(str(pid_path))} ]; do sleep 0.01; done
            : > {shlex.quote(str(ready_path))}
            while [ ! -e {shlex.quote(str(exit_path))} ]; do sleep 0.01; done
            exit 0
            """
        )
    )
    url = "ws://127.0.0.1:4501"
    container = (
        LoggedContainer(debian_image, test_name="runner-executor-escaped-output")
        .with_kwargs(
            network_mode="host",
            mem_limit="512m",
            pids_limit=64,
            user=f"{os.getuid()}:{os.getgid()}",
            working_dir=str(tmp_path),
        )
        .with_volume_mapping(str(tmp_path), str(tmp_path), mode="rw")
        .with_volume_mapping(str(execution_binary), "/execution-service", mode="ro")
        .with_command(
            shlex.join(
                [
                    "/execution-service",
                    "companion",
                    "--socket",
                    str(socket_path),
                    "--listen",
                    url,
                    "--binary",
                    "/bin/sh",
                    "--health-listen",
                    "127.0.0.1:0",
                ]
            )
        )
    )
    companion = CodexExecServerContainer(url=url, socket=socket_path, container=container, health_port=0)
    with container:
        await asyncio.to_thread(_wait_for_executor_file, socket_path)
        async with grpc.aio.insecure_channel(
            f"unix://{socket_path}", options=(("grpc.enable_retries", 0), ("grpc.use_local_subchannel_pool", 1))
        ) as channel:
            stub: control_pb2_grpc.CompanionControlAsyncStub = control_pb2_grpc.CompanionControlStub(channel)
            claim = stub.Claim(control_pb2.ClaimRequest(environment={"PATH": "/usr/bin:/bin"}))
            response = await asyncio.wait_for(claim.read(), timeout=5)
            assert isinstance(response, control_pb2.ClaimResponse)
            generation = response.generation
            assert generation
            await asyncio.to_thread(_wait_for_executor_file, ready_path)
            pipe_holder_host_pid = await asyncio.to_thread(_wait_for_host_pid, companion, marker)
            exit_path.write_text("exit\n")
            await asyncio.wait_for(wait_for_exit(pipe_holder_host_pid), timeout=10)
            idle = await _wait_for_generation(socket_path, generation, owned=False)
            assert idle.generation != generation
            assert await asyncio.wait_for(claim.read(), timeout=5) is grpc.aio.EOF


async def test_process_output_resume_files_and_incarnation_fence(runner: RunningRunner, tmp_path: Path) -> None:
    scope = executor_pb2.ExecutionScope(
        destination=identity_pb2.SandboxDestination(
            owner=identity_pb2.ServiceAccount(
                namespace=runner.config.launch.sandbox.namespace, name=runner.config.launch.sandbox.name
            ),
            target=identity_pb2.EnvironmentTarget(
                namespace=runner.config.launch.sandbox.namespace,
                name=runner.config.launch.sandbox.name,
                uid=runner.config.launch.sandbox.uid,
            ),
        ),
        expected_incarnation_uid=runner.gateway.control.incarnation_uid,
        session_id="test-execution-session",
    )
    channel = await runner.gateway.control.open_channel(runner.control_endpoint, options=(("grpc.enable_retries", 0),))
    async with channel:
        async with asyncio.timeout(30):
            await channel.channel_ready()
        control_status = await (await runner.control()).GetStatus(session_pb2.GetStatusRequest())
        metadata = (
            ("agentplane-sandbox-uid", scope.destination.target.uid),
            ("agentplane-incarnation-uid", scope.expected_incarnation_uid),
            ("agentplane-environment-runtime-boot-id", control_status.environment_runtime_boot_id),
            ("agentplane-connection-epoch", str(runner.gateway.control.connection_epoch)),
        )
        client = executor_pb2_grpc.ExecutorStub(channel)
        stale = executor_pb2.ExecutionScope()
        stale.CopyFrom(scope)
        stale.expected_incarnation_uid = "old-incarnation"
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await client.OpenSession(
                executor_pb2.OpenSessionRequest(scope=stale, request_id=uuid4().hex), metadata=metadata
            ).read()
        assert refused.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        opened = client.OpenSession(
            executor_pb2.OpenSessionRequest(scope=scope, request_id=uuid4().hex), metadata=metadata
        )
        first = await anext(aiter(opened))
        assert first.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
        session = first.execution_session_id
        process = uuid4().hex
        started = await client.StartProcess(
            executor_pb2.StartProcessRequest(
                scope=scope,
                execution_session_id=session,
                request_id=uuid4().hex,
                process_id=process,
                argv=["/bin/sh", "-c", "printf first; read line; printf '%s' \"$line\""],
                cwd=tmp_path.as_uri(),
                pipe_stdin=True,
            ),
            metadata=metadata,
            timeout=10,
        )
        assert started.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
        assert started.process_id == process
        before = bytearray()
        cursor: int | None = None
        async with asyncio.timeout(10):
            while bytes(before) != b"first":
                read = await client.ReadProcess(
                    executor_pb2.ReadProcessRequest(
                        scope=scope,
                        execution_session_id=session,
                        request_id=uuid4().hex,
                        process_id=process,
                        after_seq=cursor,
                        max_bytes=1024,
                        wait_ms=100,
                    ),
                    metadata=metadata,
                    timeout=5,
                )
                assert read.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
                before.extend(b"".join(chunk.data for chunk in read.chunks))
                cursor = read.next_seq - 1 if read.next_seq else None
        # Closing the gRPC stream detaches the native session; resume keeps the
        # process identity and output cursor without starting the command again.
        opened.cancel()
        resumed = client.OpenSession(
            executor_pb2.OpenSessionRequest(scope=scope, request_id=uuid4().hex, resume_session_id=session),
            metadata=metadata,
        )
        resumed_responses = aiter(resumed)
        response = await anext(resumed_responses)
        assert response.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
        assert response.execution_session_id == session
        written = await client.WriteProcess(
            executor_pb2.WriteProcessRequest(
                scope=scope,
                execution_session_id=session,
                request_id=uuid4().hex,
                process_id=process,
                data=b"second\n",
                write_id=uuid4().hex,
            ),
            metadata=metadata,
            timeout=10,
        )
        assert written.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
        output = bytearray()
        async with asyncio.timeout(10):
            while True:
                read = await client.ReadProcess(
                    executor_pb2.ReadProcessRequest(
                        scope=scope,
                        execution_session_id=session,
                        request_id=uuid4().hex,
                        process_id=process,
                        after_seq=cursor,
                        max_bytes=1024,
                        wait_ms=100,
                    ),
                    metadata=metadata,
                    timeout=5,
                )
                assert read.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
                output.extend(b"".join(chunk.data for chunk in read.chunks))
                cursor = read.next_seq - 1 if read.next_seq else None
                if read.exited:
                    assert read.exit_code == 0
                    break
        assert output == b"second"
        authored = tmp_path / "authored.txt"
        authored.write_bytes(b"retained authored file")
        handle = uuid4().hex
        file = await client.OpenFile(
            executor_pb2.OpenFileRequest(
                scope=scope,
                execution_session_id=session,
                request_id=uuid4().hex,
                handle_id=handle,
                path=authored.as_uri(),
            ),
            metadata=metadata,
            timeout=10,
        )
        assert file.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
        content = await client.ReadFile(
            executor_pb2.ReadFileRequest(
                scope=scope, execution_session_id=session, request_id=uuid4().hex, handle_id=handle, offset=9, length=8
            ),
            metadata=metadata,
            timeout=10,
        )
        assert content.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
        assert content.data == b"authored"
        await runner.stop()
        assert runner.process.exit_code == 0
        async with asyncio.timeout(10):
            with pytest.raises(grpc.aio.AioRpcError) as ended:
                await _drain(resumed_responses)
        assert ended.value.code() in (grpc.StatusCode.CANCELLED, grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.ABORTED)
        assert authored.read_bytes() == b"retained authored file"


async def _drain(responses: AsyncIterator[executor_pb2.OpenSessionResponse]) -> None:
    async for _ in responses:
        pass


if __name__ == "__main__":
    pytest_bazel.main()

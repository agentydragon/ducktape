"""Run the standalone service as container PID 1 with the real pinned exec-server."""

import asyncio
import json
import os
import shlex
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import grpc.aio
import pytest
import pytest_bazel

from agentplane.env.execution import executor_pb2, executor_pb2_grpc
from agentplane.proto import sandboxes_pb2
from third_party.containers import debian_slim
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def execution_container(tmp_path: Path) -> Iterator[tuple[LoggedContainer, str]]:
    load_oci_image(debian_slim.IMAGE)
    binary = get_required_path(
        own_repo_rlocation(
            "agentplane/env/runtime/execution/cmd/agentplane-execution-service_/agentplane-execution-service"
        )
    ).resolve()
    codex = get_required_path("agentplane_codex_cli_linux_x64/bin/codex").resolve()
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        endpoint = f"ws://127.0.0.1:{reserve.getsockname()[1]}"
    state = tmp_path / "s"
    config = tmp_path / "execution.json"
    config.write_text(
        json.dumps(
            {
                "namespace": "test-namespace",
                "sandbox_name": "test-sandbox",
                "sandbox_uid": "test-sandbox-uid",
                "incarnation_uid": "test-incarnation",
                "state_dir": str(state),
                "working_directory": str(tmp_path),
                "binary": "/opt/codex/bin/codex",
                "executor_url": endpoint,
                "environment": {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                "backend": "pod",
            }
        )
    )
    container = (
        LoggedContainer(debian_slim.IMAGE.tag, test_name="execution-service")
        .with_kwargs(network_mode="host", mem_limit="512m", pids_limit=64, user=f"{os.getuid()}:{os.getgid()}")
        .with_volume_mapping(str(tmp_path), str(tmp_path), mode="rw")
        .with_volume_mapping(str(binary), "/execution-service", mode="ro")
        .with_volume_mapping(str(codex.parent.parent), "/opt/codex", mode="ro")
        .with_command(shlex.join(["/execution-service", "--config", str(config)]))
    )
    with container:
        yield container, f"unix:{state / 'execution.sock'}"


def _scope() -> executor_pb2.ExecutionScope:
    return executor_pb2.ExecutionScope(
        destination=sandboxes_pb2.SandboxDestination(
            owner=sandboxes_pb2.ServiceAccount(namespace="test-namespace", name="test-sandbox"),
            sandbox="test-sandbox",
            sandbox_uid="test-sandbox-uid",
        ),
        expected_incarnation_uid="test-incarnation",
        session_id="test-session",
    )


async def test_process_output_resume_files_and_incarnation_fence(
    execution_container: tuple[LoggedContainer, str], tmp_path: Path
) -> None:
    container, target = execution_container
    scope = _scope()
    async with grpc.aio.insecure_channel(target, options=[("grpc.enable_retries", 0)]) as channel:
        async with asyncio.timeout(30):
            await channel.channel_ready()
        client = executor_pb2_grpc.ExecutorStub(channel)
        stale = _scope()
        stale.expected_incarnation_uid = "old-incarnation"
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await client.OpenSession(executor_pb2.OpenSessionRequest(scope=stale, request_id=uuid4().hex)).read()
        assert refused.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        opened = client.OpenSession(executor_pb2.OpenSessionRequest(scope=scope, request_id=uuid4().hex))
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
                    timeout=5,
                )
                assert read.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
                before.extend(b"".join(chunk.data for chunk in read.chunks))
                cursor = read.next_seq - 1 if read.next_seq else None
        # Closing the gRPC stream detaches the native session; resume keeps the
        # process identity and output cursor without starting the command again.
        opened.cancel()
        resumed = client.OpenSession(
            executor_pb2.OpenSessionRequest(scope=scope, request_id=uuid4().hex, resume_session_id=session)
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
            timeout=10,
        )
        assert file.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
        content = await client.ReadFile(
            executor_pb2.ReadFileRequest(
                scope=scope, execution_session_id=session, request_id=uuid4().hex, handle_id=handle, offset=9, length=8
            ),
            timeout=10,
        )
        assert content.status.outcome == executor_pb2.ExecutionStatus.OUTCOME_COMPLETED
        assert content.data == b"authored"
        # PID 1 exits on SIGTERM with the OpenSession still held. Its native process
        # and detached descendants are fenced by the container runtime.
        await asyncio.to_thread(container.get_wrapped_container().stop, timeout=15)
        container.get_wrapped_container().reload()
        assert container.get_wrapped_container().attrs["State"]["ExitCode"] == 0
        async with asyncio.timeout(10):
            with pytest.raises(grpc.aio.AioRpcError) as ended:
                await _drain(resumed_responses)
        assert ended.value.code() in (grpc.StatusCode.CANCELLED, grpc.StatusCode.UNAVAILABLE)
        assert authored.read_bytes() == b"retained authored file"


async def _drain(responses: AsyncIterator[executor_pb2.OpenSessionResponse]) -> None:
    async for _ in responses:
        pass


if __name__ == "__main__":
    pytest_bazel.main()

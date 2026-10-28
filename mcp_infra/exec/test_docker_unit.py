from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.resources.template import match_uri_template
from tenacity import AsyncRetrying, retry_if_result, stop_after_delay, wait_fixed

from mcp_infra.exec.docker import container_session
from mcp_infra.exec.docker.container_session import ContainerSessionState, run_session_container, scoped_container
from mcp_infra.exec.docker.server import FILE_RESOURCE_URI_TEMPLATE, ContainerExecServer
from mcp_infra.exec.docker.types import AlwaysSetTo, BindMount, ContainerExecServerConfig, ResolvedExecInput
from mcp_infra.testing.fixtures import make_container_opts
from util.exec.models import BaseExecResult, Exited, TimedOut


@pytest.fixture
def exec_server(async_docker_client, debian_slim_image):
    """Container exec server for docker exec tests."""
    return ContainerExecServer(async_docker_client, make_container_opts(debian_slim_image))


@pytest.fixture
async def exec_session(exec_server):
    async with Client(exec_server) as session:
        yield session


async def _call_exec(session: Client, cmd: list[str], *, timeout_ms: int = 5000) -> BaseExecResult:
    """Call exec tool. Includes all fields for the default fixture (allow_env/user=True)."""
    args = {"cmd": cmd, "timeout_ms": timeout_ms, "env": None, "user": None, "cwd": None}
    result = await session.call_tool("exec", args)
    return BaseExecResult.model_validate(result.structured_content)


async def test_exec_stdout_stderr_timeout(exec_session) -> None:
    # stdout
    r1 = await _call_exec(exec_session, ["/bin/echo", "hello"])
    assert r1.exit == Exited(exit_code=0)
    assert isinstance(r1.stdout, str)
    assert r1.stdout == "hello\n"
    # stderr and nonzero exit
    r2 = await _call_exec(exec_session, ["sh", "-lc", "echo err 1>&2; exit 3"])
    assert r2.exit == Exited(exit_code=3)
    assert "err" in (r2.stderr or "")
    # timeout
    r3 = await _call_exec(exec_session, ["sh", "-lc", "sleep 5"], timeout_ms=500)
    assert r3.exit == TimedOut()


async def test_persession_exec_timeout_then_next_ok(exec_session) -> None:
    # Force timeout
    t1 = await _call_exec(exec_session, ["sh", "-lc", "printf 'started\\n'; exec sleep 30"], timeout_ms=500)
    assert t1.exit == TimedOut()
    assert t1.stdout == "started\n"
    # The session container remains usable.
    r1 = await _call_exec(exec_session, ["/bin/echo", "ok"])
    assert r1.exit == Exited(exit_code=0)
    assert isinstance(r1.stdout, str)
    assert r1.stdout == "ok\n"


@pytest.mark.parametrize("error_type", [ValueError, TimeoutError])
async def test_collector_error_propagates(async_docker_client, debian_slim_image, monkeypatch, error_type) -> None:
    async def fail_collect(stream, stdout_buf, stderr_buf):
        chunk = await stream.read_out()
        assert chunk is not None
        assert chunk.data == b"started\n"
        raise error_type("collector failed")

    monkeypatch.setattr(container_session, "_collect_from_exec_stream", fail_collect)
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        input = ResolvedExecInput(
            cmd=["sh", "-lc", "echo started; exec sleep 30"], cwd=None, env=None, user=None, timeout_ms=5000
        )
        with pytest.raises(error_type, match="collector failed"):
            await run_session_container(session, input.cmd, input)


async def test_external_cancellation_closes_stream(async_docker_client, debian_slim_image, monkeypatch) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()
    original_collect = container_session._collect_from_exec_stream

    async def collect_after_first_output(stream, stdout_buf, stderr_buf):
        original_close = stream.close

        async def close():
            await original_close()
            closed.set()

        stream.close = close
        chunk = await stream.read_out()
        assert chunk is not None
        assert chunk.data == b"started\n"
        started.set()
        await original_collect(stream, stdout_buf, stderr_buf)

    monkeypatch.setattr(container_session, "_collect_from_exec_stream", collect_after_first_output)
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        input = ResolvedExecInput(
            cmd=["sh", "-lc", "echo started; exec sleep 30"], cwd=None, env=None, user=None, timeout_ms=60000
        )
        task = asyncio.create_task(run_session_container(session, input.cmd, input))
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()


async def _proc_cmdline(session: ContainerSessionState, pid: int) -> bytes:
    """A dead process has an absent or empty cmdline, including unreaped zombies."""
    input = ResolvedExecInput(
        cmd=["sh", "-c", f"cat /proc/{pid}/cmdline 2>/dev/null || true"], cwd=None, env=None, user=None, timeout_ms=5000
    )
    stdout, _, exit_code, timed_out = await run_session_container(session, input.cmd, input)
    assert not timed_out
    assert exit_code == 0
    return bytes(stdout)


async def test_deadline_kills_parent_and_child_but_preserves_sibling(async_docker_client, debian_slim_image) -> None:
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        container = await async_docker_client.containers.get(container_id)
        sibling = await container.exec(["sh", "-c", "echo $$; exec sleep 30"], stdout=True)
        async with sibling.start() as sibling_stream:
            chunk = await sibling_stream.read_out()
            assert chunk is not None
            sibling_pid = int(chunk.data)
            input = ResolvedExecInput(
                cmd=["sh", "-c", "sleep 30 & echo $$ $!; wait"], cwd=None, env=None, user=None, timeout_ms=300
            )
            stdout, _, exit_code, timed_out = await run_session_container(session, input.cmd, input)
            assert timed_out
            assert exit_code is None
            parent_pid, child_pid = map(int, stdout.split())
            assert await _proc_cmdline(session, parent_pid) == b""
            assert await _proc_cmdline(session, child_pid) == b""
            assert await _proc_cmdline(session, sibling_pid) == b"sleep\x0030\x00"


async def test_cancellation_leaves_remote_deadline_in_force(
    async_docker_client, debian_slim_image, monkeypatch
) -> None:
    started = asyncio.Event()
    pid_data = bytearray()
    original_collect = container_session._collect_from_exec_stream

    async def collect_after_pid(stream, stdout_buf, stderr_buf):
        chunk = await stream.read_out()
        assert chunk is not None
        pid_data.extend(chunk.data)
        started.set()
        await original_collect(stream, stdout_buf, stderr_buf)

    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        input = ResolvedExecInput(
            cmd=["sh", "-c", "echo $$; exec sleep 30"], cwd=None, env=None, user=None, timeout_ms=1000
        )
        monkeypatch.setattr(container_session, "_collect_from_exec_stream", collect_after_pid)
        task = asyncio.create_task(run_session_container(session, input.cmd, input))
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(container_session, "_collect_from_exec_stream", original_collect)
        pid = int(pid_data)
        cmdline: bytes = await AsyncRetrying(
            retry=retry_if_result(bool), wait=wait_fixed(0.05), stop=stop_after_delay(5)
        )(_proc_cmdline, session, pid)
        assert cmdline == b""


@pytest.mark.parametrize("exit_code", [124, 125, 137])
async def test_early_command_exit_status_is_preserved(exec_session, exit_code: int) -> None:
    result = await _call_exec(exec_session, ["sh", "-c", f"exit {exit_code}"])
    assert result.exit == Exited(exit_code=exit_code)


async def test_timeout_preserves_literal_arguments(exec_session) -> None:
    result = await _call_exec(exec_session, ["/usr/bin/printf", "%s\\n", "$HOME; echo surprise"])
    assert result.exit == Exited(exit_code=0)
    assert result.stdout == "$HOME; echo surprise\n"


def test_from_config_roundtrip() -> None:
    """ContainerExecServerConfig → from_config produces correct opts and kwargs."""
    config = ContainerExecServerConfig(
        image="test:latest",
        working_dir=Path("/work"),
        binds=[BindMount(host_path=Path("/host/dir"), container_path=Path("/container/dir"), mode="ro")],
        network_mode="bridge",
        environment={"FOO": "bar"},
        labels={"app": "test"},
        allow_user_field=True,
        allow_env_field=False,
        cwd_policy=AlwaysSetTo(value=Path("/work")),
    )

    # Verify JSON roundtrip preserves discriminated union
    restored = ContainerExecServerConfig.model_validate_json(config.model_dump_json())
    assert isinstance(restored.cwd_policy, AlwaysSetTo)
    assert restored.cwd_policy.value == Path("/work")
    assert restored.binds[0].mode == "ro"
    assert restored.allow_user_field is True
    assert restored.allow_env_field is False


def test_file_uri_template_matches_paths_with_slashes() -> None:
    """FILE_RESOURCE_URI_TEMPLATE must match paths containing slashes.

    The template uses RFC 6570 wildcard syntax {path*} so the regex uses .+
    instead of [^/]+ - allowing it to match absolute paths like /init.
    """
    # Root-level file (the failing case before the fix)
    result = match_uri_template("file:///init", FILE_RESOURCE_URI_TEMPLATE)
    assert result is not None
    assert result["path"] == "/init"

    # Nested path
    result = match_uri_template("file:///foo/bar/baz.txt", FILE_RESOURCE_URI_TEMPLATE)
    assert result is not None
    assert result["path"] == "/foo/bar/baz.txt"

    # file_uri helper produces matching URIs
    uri = ContainerExecServer.file_uri("/some/path.txt")
    result = match_uri_template(uri, FILE_RESOURCE_URI_TEMPLATE)
    assert result is not None
    assert result["path"] == "/some/path.txt"


if __name__ == "__main__":
    pytest_bazel.main()

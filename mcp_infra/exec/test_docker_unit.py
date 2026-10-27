from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_bazel
from aiodocker.containers import DockerContainer
from aiodocker.stream import Stream
from fastmcp.client import Client
from fastmcp.resources.template import match_uri_template
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

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
    t1 = await _call_exec(exec_session, ["sh", "-lc", "printf 'started\\n'; echo $$; exec sleep 30"], timeout_ms=500)
    assert t1.exit == TimedOut()
    assert isinstance(t1.stdout, str)
    assert t1.stdout.startswith("started\n")
    # Next call should succeed after restart
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
    pid_data = bytearray()
    original_collect = container_session._collect_from_exec_stream

    async def collect_after_first_output(stream, stdout_buf, stderr_buf):
        original_close = stream.close

        async def close():
            await original_close()
            closed.set()

        stream.close = close
        chunk = await stream.read_out()
        assert chunk is not None
        pid_data.extend(chunk.data)
        started.set()
        await original_collect(stream, stdout_buf, stderr_buf)

    monkeypatch.setattr(container_session, "_collect_from_exec_stream", collect_after_first_output)
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        input = ResolvedExecInput(
            cmd=["sh", "-lc", "echo $$; exec sleep 120"], cwd=None, env=None, user=None, timeout_ms=60000
        )
        task = asyncio.create_task(run_session_container(session, input.cmd, input))
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        monkeypatch.setattr(container_session, "_collect_from_exec_stream", original_collect)
        pid = int(pid_data.decode().strip())
        assert not await _proc_exists(session, pid)


async def _proc_cmdline(session: ContainerSessionState, pid: int) -> bytes:
    input = ResolvedExecInput(
        cmd=["sh", "-c", f"cat /proc/{pid}/cmdline 2>/dev/null || true"], cwd=None, env=None, user=None, timeout_ms=5000
    )
    stdout, _, exit_code, timed_out = await run_session_container(session, input.cmd, input)
    assert exit_code == 0
    assert not timed_out
    return bytes(stdout)


async def _proc_exists(session: ContainerSessionState, pid: int) -> bool:
    input = ResolvedExecInput(cmd=["sh", "-c", f"test -e /proc/{pid}"], cwd=None, env=None, user=None, timeout_ms=5000)
    _, _, exit_code, timed_out = await run_session_container(session, input.cmd, input)
    assert not timed_out
    return exit_code == 0


async def test_timeout_kills_parent_and_child(async_docker_client, debian_slim_image) -> None:
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        input = ResolvedExecInput(
            cmd=["sh", "-c", "sleep 120 & echo $$ $!; wait"], cwd=None, env=None, user=None, timeout_ms=300
        )
        stdout, _, exit_code, timed_out = await run_session_container(session, input.cmd, input)
        assert timed_out
        assert exit_code is None
        parent_pid, child_pid = map(int, stdout.decode().split())
        assert not await _proc_exists(session, parent_pid)
        assert not await _proc_exists(session, child_pid)


async def test_timeout_preserves_sibling_exec(async_docker_client, debian_slim_image) -> None:
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        container = await async_docker_client.containers.get(container_id)
        sibling = await container.exec(["sh", "-c", "echo $$; exec sleep 120"], stdout=True, stderr=True)
        sibling_stream = sibling.start()
        chunk = await sibling_stream.read_out()
        assert chunk is not None
        sibling_pid = int(chunk.data.decode().strip())
        input = ResolvedExecInput(
            cmd=["sh", "-c", "echo $$; exec sleep 120"], cwd=None, env=None, user=None, timeout_ms=300
        )
        stdout, _, _, timed_out = await run_session_container(session, input.cmd, input)
        assert timed_out
        target_pid = int(stdout.decode().strip())
        assert not await _proc_exists(session, target_pid)
        assert await _proc_cmdline(session, sibling_pid) == b"sleep\x00120\x00"
        await sibling_stream.close()


async def test_leader_exit_with_child_holding_stdout_times_out(async_docker_client, debian_slim_image) -> None:
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        input = ResolvedExecInput(
            cmd=["sh", "-c", "sleep 120 & echo $!; exit 0"], cwd=None, env=None, user=None, timeout_ms=300
        )
        stdout, _, _, timed_out = await run_session_container(session, input.cmd, input)
        assert timed_out
        child_pid = int(stdout.decode().strip())
        assert not await _proc_exists(session, child_pid)


async def test_redirected_background_returns_normally(async_docker_client, debian_slim_image) -> None:
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        input = ResolvedExecInput(
            cmd=["sh", "-c", "sleep 120 >/dev/null 2>&1 & echo $!"], cwd=None, env=None, user=None, timeout_ms=5000
        )
        stdout, _, exit_code, timed_out = await run_session_container(session, input.cmd, input)
        assert not timed_out
        assert exit_code == 0
        child_pid = int(stdout.decode().strip())
        assert await _proc_cmdline(session, child_pid) == b"sleep\x00120\x00"


async def test_exec_preserves_argv_env_user_cwd_and_exit(async_docker_client, debian_slim_image) -> None:
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        input = ResolvedExecInput(
            cmd=["sh", "-c", 'printf "%s|%s|%s\\n" "$PROBE" "$(pwd)" "$(id -u)"; echo err >&2; exit 7'],
            cwd="/tmp",
            env=["PROBE=value with spaces"],
            user="65534",
            timeout_ms=5000,
        )
        stdout, stderr, exit_code, timed_out = await run_session_container(session, input.cmd, input)
        assert not timed_out
        assert exit_code == 7
        assert stdout == b"value with spaces|/tmp|65534\n"
        assert stderr == b"err\n"
        literal = ResolvedExecInput(
            cmd=["/usr/bin/printf", "%s\\n", "$PROBE"], cwd=None, env=None, user=None, timeout_ms=5000
        )
        stdout, _, exit_code, timed_out = await run_session_container(session, literal.cmd, literal)
        assert not timed_out
        assert exit_code == 0
        assert stdout == b"$PROBE\n"

        missing = ResolvedExecInput(cmd=["/no/such/command"], cwd=None, env=None, user=None, timeout_ms=5000)
        _, stderr, exit_code, timed_out = await run_session_container(session, missing.cmd, missing)
        assert not timed_out
        assert exit_code == 127
        assert b"no such file" in stderr.lower()


@pytest.mark.parametrize("pause_before_post", [True, False])
async def test_cancel_during_lazy_stream_start_cleans_accepted_exec(
    async_docker_client, debian_slim_image, monkeypatch, pause_before_post
) -> None:
    opts = make_container_opts(debian_slim_image)
    async with scoped_container(async_docker_client, opts) as container_id:
        session = ContainerSessionState(async_docker_client, container_id, opts)
        container = await async_docker_client.containers.get(container_id)
        original_init = Stream._init
        original_exec = DockerContainer.exec
        created_exec = []

        async def capture_exec(self, command, **kwargs):
            exec_obj = await original_exec(self, command, **kwargs)
            if command[0].startswith("/.ducktape_docker_exec_supervisor_"):
                created_exec.append(exec_obj)
            return exec_obj

        monkeypatch.setattr(DockerContainer, "exec", capture_exec)
        accepted = asyncio.Event()
        release = asyncio.Event()

        async def pause_after_start(self):
            if pause_before_post and not accepted.is_set():
                accepted.set()
                await release.wait()
            await original_init(self)
            if not pause_before_post and not accepted.is_set():
                accepted.set()
                await release.wait()

        monkeypatch.setattr(Stream, "_init", pause_after_start)
        input = ResolvedExecInput(
            cmd=["sh", "-c", "echo $$ >/tmp/docker-exec-race-pid; exec sleep 120"],
            cwd=None,
            env=None,
            user=None,
            timeout_ms=60000,
        )
        task = asyncio.create_task(run_session_container(session, input.cmd, input))
        await asyncio.wait_for(accepted.wait(), 10)

        async def read_pid() -> int:
            probe = await container.exec(["sh", "-c", "cat /tmp/docker-exec-race-pid 2>/dev/null"], stdout=True)
            stream = probe.start()
            chunks = bytearray()
            while (chunk := await stream.read_out()) is not None:
                chunks.extend(chunk.data)
            assert chunks, "Docker exec has not written its PID yet"
            return int(chunks.decode().strip())

        if not pause_before_post:
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(10),
                wait=wait_fixed(0.05),
                retry=retry_if_exception_type(AssertionError),
                reraise=True,
            ):
                with attempt:
                    pid = await read_pid()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(created_exec) == 1
        assert not (await created_exec[0].inspect())["Running"]
        if pause_before_post:
            try:
                pid = await read_pid()
            except AssertionError:
                return
        assert not await _proc_exists(session, pid)


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

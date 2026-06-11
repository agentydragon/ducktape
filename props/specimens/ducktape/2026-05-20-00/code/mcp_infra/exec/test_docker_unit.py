from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.resources.template import match_uri_template

from mcp_infra.exec.docker.server import FILE_RESOURCE_URI_TEMPLATE, ContainerExecServer
from mcp_infra.exec.docker.types import AlwaysSetTo, BindMount, ContainerExecServerConfig
from mcp_infra.exec.models import BaseExecResult, Exited, TimedOut
from mcp_infra.testing.fixtures import make_container_opts


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
    t1 = await _call_exec(exec_session, ["sh", "-lc", "sleep 3"], timeout_ms=500)
    assert t1.exit == TimedOut()
    # Next call should succeed after restart
    r1 = await _call_exec(exec_session, ["/bin/echo", "ok"])
    assert r1.exit == Exited(exit_code=0)
    assert isinstance(r1.stdout, str)
    assert r1.stdout == "ok\n"


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

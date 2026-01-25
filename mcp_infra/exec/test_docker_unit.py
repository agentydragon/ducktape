from __future__ import annotations

import pytest
import pytest_bazel
from fastmcp.resources.template import match_uri_template

from mcp_infra.exec.docker.server import FILE_RESOURCE_URI_TEMPLATE, ContainerExecServer
from mcp_infra.exec.models import Exited, TimedOut, make_exec_input
from mcp_infra.testing.fixtures import make_container_opts


@pytest.fixture
def exec_server(async_docker_client):
    """Container exec server for docker exec tests."""
    return ContainerExecServer(async_docker_client, make_container_opts("python:3.12-slim"))


@pytest.fixture
async def exec_client(make_typed_mcp, exec_server):
    async with make_typed_mcp(exec_server) as (client, _session):
        yield client


@pytest.mark.requires_docker
async def test_exec_stdout_stderr_timeout(exec_client) -> None:
    # stdout
    r1 = await exec_client.exec(make_exec_input(["/bin/echo", "hello"], timeout_ms=5000))
    assert r1.exit == Exited(exit_code=0)
    assert isinstance(r1.stdout, str)  # Short output should not be truncated
    assert r1.stdout == "hello\n"
    # stderr and nonzero exit
    r2 = await exec_client.exec(make_exec_input(["sh", "-lc", "echo err 1>&2; exit 3"], timeout_ms=5000))
    assert r2.exit == Exited(exit_code=3)
    assert "err" in (r2.stderr or "")
    # timeout
    r3 = await exec_client.exec(make_exec_input(["sh", "-lc", "sleep 5"], timeout_ms=500))
    assert r3.exit == TimedOut()


@pytest.mark.requires_docker
async def test_persession_exec_timeout_then_next_ok(exec_client) -> None:
    # Force timeout
    t1 = await exec_client.exec(make_exec_input(["sh", "-lc", "sleep 3"], timeout_ms=500))
    assert t1.exit == TimedOut()
    # Next call should succeed after restart
    r1 = await exec_client.exec(make_exec_input(["/bin/echo", "ok"], timeout_ms=5000))
    assert r1.exit == Exited(exit_code=0)
    assert isinstance(r1.stdout, str)  # Short output should not be truncated
    assert r1.stdout == "ok\n"


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

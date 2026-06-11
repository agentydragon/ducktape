from __future__ import annotations

import pytest

from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.models import BaseExecResult, Exited, TimedOut, make_exec_input
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.stubs.typed_stubs import ToolStub
from mcp_infra.testing.fixtures import make_container_opts


def _runtime_spec_persession(docker_client, image: str = "python:3.12-slim"):
    return ContainerExecServer(
        docker_client,
        make_container_opts(image),  # per-session container
    )


@pytest.mark.requires_docker
async def test_runtime_per_session_timeout_then_next_call_ok(
    compositor, compositor_client, async_docker_client
) -> None:
    """Test runtime timeout and recovery without policy gateway."""
    # Mount runtime server and capture Mounted object
    mounted_runtime = await compositor.mount_inproc(
        MCPMountPrefix("runtime"), _runtime_spec_persession(async_docker_client)
    )

    # Cause a host-side timeout: sleep longer than timeout_ms
    # Namespaced exec via Compositor
    stub = ToolStub(
        compositor_client,
        build_mcp_function(mounted_runtime.prefix, mounted_runtime.server.exec_tool.name),
        BaseExecResult,
    )

    res_timeout = await stub(make_exec_input(["sh", "-lc", "sleep 3"], timeout_ms=500))
    assert isinstance(res_timeout.exit, TimedOut)

    # Next call should work; container should have been restarted
    res_ok = await stub(make_exec_input(["/bin/echo", "-n", "ok"], timeout_ms=5000))
    assert isinstance(res_ok.exit, Exited)
    assert res_ok.exit.exit_code == 0
    assert (res_ok.stdout or "") == "ok"

import pytest
import pytest_bazel
from fastmcp.client import Client

from mcp_infra.exec.direct import DirectExecServer
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.models import BaseExecResult, Exited
from mcp_infra.exec.subprocess import DirectExecArgs
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.stubs.typed_stubs import ToolStub
from mcp_infra.testing.simple_servers import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME


async def test_stdio_server_list_tools(compositor, compositor_client, stdio_echo_spec) -> None:
    """Smoke test: mount stdio server and list tools."""
    await compositor.mount_server(ECHO_MOUNT_PREFIX, stdio_echo_spec)
    tools = await compositor_client.list_tools()
    tool_names = {t.name for t in tools}
    # Echo server exposes exactly one tool
    expected = build_mcp_function(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME)
    assert expected in tool_names


async def test_direct_inprocess_server(compositor, compositor_client) -> None:
    """Direct (unsandboxed) in-process FastMCP exec tool mounted in a Compositor."""

    srv = DirectExecServer()
    await compositor.mount_inproc(MCPMountPrefix("local"), srv)

    tools = await compositor_client.list_tools()
    # Tools are composed under the compositor with namespaced tool names
    tool_name = build_mcp_function(MCPMountPrefix("local"), "exec")
    assert any(t.name == tool_name for t in tools)
    # Sanity-call exec via the namespaced tool using the typed helper
    exec_stub = ToolStub(compositor_client, tool_name, BaseExecResult)
    result = await exec_stub(DirectExecArgs(cmd=["/bin/echo", "hello"], max_bytes=100_000, timeout_ms=5000))
    # Compare whole exit object
    assert result.exit == Exited(exit_code=0)


@pytest.mark.requires_docker
async def test_inproc_container_exec_exposes_container_info_resource(
    docker_exec_server_py312slim: ContainerExecServer,
) -> None:
    """in-proc container exec exposes a container.info resource."""

    # Call the server directly to read the resource; no manager needed here
    async with Client(docker_exec_server_py312slim) as sess:
        res = await sess.read_resource_mcp(docker_exec_server_py312slim.container_info_resource.uri)
        assert res.contents, "container.info returned no contents"


if __name__ == "__main__":
    pytest_bazel.main()

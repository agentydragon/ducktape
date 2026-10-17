import pytest_bazel
from fastmcp.client import Client

from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.naming import build_mcp_function
from mcp_infra.testing.simple_servers import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME


async def test_stdio_server_list_tools(compositor, compositor_client, stdio_echo_spec) -> None:
    """Smoke test: mount stdio server and list tools."""
    await compositor.mount_server(ECHO_MOUNT_PREFIX, stdio_echo_spec)
    tools = await compositor_client.list_tools()
    tool_names = {t.name for t in tools}
    # Echo server exposes exactly one tool
    expected = build_mcp_function(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME)
    assert expected in tool_names


async def test_inproc_container_exec_exposes_container_info_resource(docker_exec_server: ContainerExecServer) -> None:
    """in-proc container exec exposes a container.info resource."""

    # Call the server directly to read the resource; no manager needed here
    async with Client(docker_exec_server) as sess:
        res = await sess.read_resource_mcp(ContainerExecServer.CONTAINER_INFO_URI)
        assert res.contents, "container.info returned no contents"


if __name__ == "__main__":
    pytest_bazel.main()

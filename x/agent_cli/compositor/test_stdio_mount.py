import pytest_bazel

from mcp_infra.naming import build_mcp_function
from mcp_infra.testing.simple_servers import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME


async def test_stdio_server_list_tools(compositor, compositor_client, stdio_echo_spec) -> None:
    """Mount a stdio server and expose its prefixed tool through the compositor."""
    await compositor.mount_server(ECHO_MOUNT_PREFIX, stdio_echo_spec)
    tools = await compositor_client.list_tools()
    tool_names = {t.name for t in tools}
    expected = build_mcp_function(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME)
    assert expected in tool_names


if __name__ == "__main__":
    pytest_bazel.main()

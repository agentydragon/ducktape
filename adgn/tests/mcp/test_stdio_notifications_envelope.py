from __future__ import annotations

from pathlib import Path
import sys
import textwrap

from fastmcp.client import Client
from fastmcp.mcp_config import StdioMCPServer
from tests.util.notifications import parse_system_notification_payload

from adgn.agent.reducer import format_notifications_message
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.compositor.server import Compositor
from adgn.mcp.notifications.buffer import NotificationsBuffer


async def test_stdio_child_notifications_envelope(tmp_path: Path):
    # Write a tiny stdio MCP server that emits notifications on a tool call
    server_py = tmp_path / "stdio_child_server.py"
    server_py.write_text(
        textwrap.dedent(
            """
            import anyio
            from adgn.mcp.notifying_fastmcp import NotifyingFastMCP
            from fastmcp.server.server import stdio_server

            m = NotifyingFastMCP("stdio_child")

            @m.tool(name="emit")
            async def emit():  # type: ignore[empty-body]
                await m.broadcast_resource_list_changed()
                await m.broadcast_resource_updated("resource://dummy")
                return True

            if __name__ == "__main__":
                anyio.run(stdio_server)
            """
        ),
        encoding="utf-8",
    )

    spec = StdioMCPServer(command=sys.executable, args=[str(server_py)])
    comp = Compositor("comp")
    await comp.mount_server("stdio_child", spec)

    buf = NotificationsBuffer(compositor=comp)
    async with Client(comp, message_handler=buf.handler) as sess:
        # Fire notifications via namespaced tool
        await sess.call_tool(name=build_mcp_function("stdio_child", "emit"), arguments={})
        batch = buf.poll()
        msg = format_notifications_message(batch)
        assert msg is not None
        payload = parse_system_notification_payload(msg)
        resources = payload.get("resources")
        assert isinstance(resources, dict)
        assert "stdio_child" in resources
        child = resources["stdio_child"]
        assert isinstance(child, dict)
        # Should include list_changed and updated with the dummy URI
        assert child.get("list_changed") is True
        assert "resource://dummy" in (child.get("updated") or [])

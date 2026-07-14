import asyncio

import pytest_bazel
from fastmcp.client.client import CallToolResult as FastMCPCallToolResult
from mcp import types as mcp_types

from haku.console.tools.tana import _read_node_preview, node_name_from_markdown


def test_node_name_from_markdown_reads_the_matching_node_marker() -> None:
    markdown = "- [ ] First node <!-- node-id: other -->\n- Target node #Task <!-- node-id: target -->"
    assert node_name_from_markdown(markdown, "target") == "Target node #Task"


def test_node_name_from_markdown_returns_none_without_the_requested_marker() -> None:
    assert node_name_from_markdown("- A node <!-- node-id: other -->", "target") is None


def test_read_node_preview_uses_a_depth_zero_read_and_ignores_unresolved_nodes() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        async def call_tool(self, name: str, arguments: dict[str, object]) -> FastMCPCallToolResult:
            calls.append((name, arguments))
            return FastMCPCallToolResult(
                content=[mcp_types.TextContent(type="text", text="- Target <!-- node-id: target -->")],
                structured_content=None,
                meta=None,
            )

    preview = asyncio.run(_read_node_preview(Client(), "target"))
    assert preview is not None
    assert preview.name == "Target"
    assert calls == [("read_node", {"nodeId": "target", "maxDepth": 0})]


if __name__ == "__main__":
    pytest_bazel.main()

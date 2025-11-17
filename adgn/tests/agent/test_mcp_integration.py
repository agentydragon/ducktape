import asyncio
import shutil

from fastmcp.client import Client
from fastmcp.mcp_config import StdioMCPServer
import pytest

from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.exec.direct import DirectExecArgs, make_direct_exec_server
from adgn.mcp.exec.models import BaseExecResult, Exited
from adgn.mcp.testing.typed_stubs import call_tool_typed

# FastMCP stdio client (hard import)


async def test_stdio_server_list_tools(make_compositor) -> None:
    """Smoke test: connect to server-everything (stdio) and list tools.

    Skips if npx or FastMCP stdio client are unavailable.
    """
    if shutil.which("npx") is None:
        pytest.skip("npx not found in PATH; required for server-everything")

    # Preflight: verify server-everything can start (help) quickly; skip if not
    try:
        proc = await asyncio.create_subprocess_exec(
            "npx",
            "--yes",
            "@modelcontextprotocol/server-everything",
            "stdio",
            "--help",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.wait(), timeout=20)
    except Exception as e:  # pragma: no cover - infra-dependent
        pytest.skip(f"preflight failed: {e}")
    if proc.returncode != 0:
        pytest.skip(f"server-everything stdio help failed (rc={proc.returncode})")

    spec = StdioMCPServer(command="npx", args=["@modelcontextprotocol/server-everything", "stdio"])

    async with make_compositor({"everything": spec}) as (sess, comp):
        tools = await sess.list_tools()
        assert isinstance(tools, list)
        assert any(t.name.startswith("everything_") for t in tools)


async def test_direct_inprocess_server(make_compositor) -> None:
    """Direct (unsandboxed) in-process FastMCP exec tool mounted in a Compositor."""

    srv = make_direct_exec_server("local")
    async with make_compositor({"local": srv}) as (sess, comp):
        tools = await sess.list_tools()
        # Tools are composed under the compositor with namespaced tool names
        tool_name = build_mcp_function("local", "exec")
        assert any(t.name == tool_name for t in tools)
        # Sanity-call exec via the namespaced tool using the typed helper
        payload = DirectExecArgs(cmd=["/bin/echo", "hello"], max_bytes=100_000, timeout_ms=5000)
        result = await call_tool_typed(sess, tool_name, payload, BaseExecResult)
        # Compare whole exit object
        assert result.exit == Exited(exit_code=0)


@pytest.mark.requires_docker
async def test_inproc_container_exec_exposes_container_info_resource(docker_inproc_spec_py312: object) -> None:
    """in-proc container exec exposes a container.info resource."""

    # Call the server directly to read the resource; no manager needed here
    async with Client(docker_inproc_spec_py312) as sess:
        res = await sess.read_resource_mcp("resource://container.info")
        assert res.contents, "container.info returned no contents"

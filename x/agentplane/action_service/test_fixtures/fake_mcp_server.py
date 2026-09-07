"""Standalone stdio MCP server for the ambiguous-transport-loss test scenario.

Run as a subprocess (never in-process) so the test can kill the transport mid-call and observe
that the Action Service records `execution_unknown` rather than retrying.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("fake-mcp-server")


@mcp.tool
def slow_echo(marker_path: str, seconds: float, text: str) -> dict[str, str]:
    Path(marker_path).write_text("started")
    time.sleep(seconds)
    return {"echoed": text}


if __name__ == "__main__":
    mcp.run()

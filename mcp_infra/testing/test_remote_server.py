"""The HTTP endpoint exposed by the MCP test server helper."""

import pytest_bazel
from fastmcp import FastMCP

from mcp_infra.testing.remote_server import serve_fastmcp


def test_fastmcp_helper_yields_the_mcp_path() -> None:
    with serve_fastmcp(FastMCP("test")) as url:
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/mcp")


if __name__ == "__main__":
    pytest_bazel.main()

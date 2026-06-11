from __future__ import annotations

import anyio

from mcp_infra.testing.simple_servers import make_simple_mcp


def main() -> None:
    """Run the simple FastMCP server over stdio.

    This mirrors the old ``adgn.mcp.echo`` helper so existing demos/tests can
    launch it via ``python -m adgn.mcp.testing.stdio_app``.
    """

    server = make_simple_mcp()
    anyio.run(server.run_stdio_async)


if __name__ == "__main__":
    main()

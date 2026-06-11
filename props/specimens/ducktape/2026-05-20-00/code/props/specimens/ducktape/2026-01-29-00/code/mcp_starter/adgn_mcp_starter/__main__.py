"""Entry point for MCP server - supports both stdio and streamable HTTP modes."""

import argparse
import logging
import sys

from mcp_starter.adgn_mcp_starter.server import create_mcp_server

logger = logging.getLogger(__name__)


def main() -> None:
    """Main entry point with transport selection."""
    parser = argparse.ArgumentParser(description="MCP Starter Template Server")
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio", help="Transport mode (default: stdio)"
    )
    parser.add_argument("--host", default="localhost", help="Host for HTTP mode (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP mode (default: 8000)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--debug-mcp", action="store_true", help="Enable full MCP request/response logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,  # Log to stderr so it doesn't interfere with stdio transport
    )
    server = create_mcp_server(debug_mcp=args.debug_mcp)

    if args.transport == "stdio":
        logger.info("Starting MCP server in STDIO mode")
        server.run("stdio")
        return

    logger.info(f"Starting MCP server in streamable HTTP mode on {args.host}:{args.port}")
    server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()

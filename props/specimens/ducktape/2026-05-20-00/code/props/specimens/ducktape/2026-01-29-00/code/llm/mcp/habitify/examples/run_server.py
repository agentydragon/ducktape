"""Run the Habitify MCP server.

Usage: bazel run //llm/mcp/habitify:run_server -- [--transport stdio|sse] [--port PORT]
"""

import argparse
import logging
import sys

from habitify.server import create_habitify
from habitify.utils.cli_utils import get_api_key_from_param_or_env

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("habitify-mcp-example")


# Parse command line arguments
def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run the Habitify MCP server")
    parser.add_argument(
        "--transport", choices=["stdio", "sse"], default="stdio", help="Transport type (stdio or sse, default: stdio)"
    )
    parser.add_argument("--port", type=int, default=3000, help="Port to use for SSE transport (default: 3000)")
    parser.add_argument("--api-key", help="Habitify API key (overrides HABITIFY_API_KEY environment variable)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> int:
    """Main entry point for the script."""
    # Parse command-line arguments
    args = parse_args()

    # Configure logging level
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    # Load API key from CLI param or environment
    api_key = get_api_key_from_param_or_env(args.api_key)
    if not api_key:
        logger.error("HABITIFY_API_KEY is required. Set in environment or pass --api-key.")
        return 1

    # Import here so we only import after checking API key

    try:
        # Create the server (with port configuration)
        logger.info("Creating Habitify MCP server...")
        server = create_habitify(port=args.port)

        # Run the server with the specified transport
        if args.transport == "stdio":
            logger.info("Starting server with stdio transport...")
            server.run(transport="stdio")
        else:
            logger.info(f"Starting server with SSE transport on port {args.port}...")
            server.run(transport="sse")  # Port is already configured in the server

        return 0
    except KeyboardInterrupt:
        logger.info("Server stopped by keyboard interrupt")
        return 0
    except Exception as e:
        logger.error(f"Error running server: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())

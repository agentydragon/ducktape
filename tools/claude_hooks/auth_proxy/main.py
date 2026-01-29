"""CLI entry point for running the auth proxy.

This script is invoked by supervisor to run the proxy as a long-running service.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path
from types import FrameType

from tools.claude_hooks.auth_proxy.proxy import AuthForwardingProxy
from tools.claude_hooks.debug import log_entrypoint_debug

logger = logging.getLogger(__name__)


def main() -> int:
    """Run the auth proxy."""
    parser = argparse.ArgumentParser(description="Run auth proxy for Bazel")
    parser.add_argument("--listen-port", type=int, required=True, help="Local port to listen on")
    parser.add_argument("--creds-file", type=Path, required=True, help="Path to file containing upstream proxy URL")
    parser.add_argument("--log-level", default="INFO", help="Logging level")

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    log_entrypoint_debug("auth_proxy")

    # Validate creds file exists
    if not args.creds_file.exists():
        logger.error("Credentials file not found: %s", args.creds_file)
        return 1

    # Create and start proxy
    proxy = AuthForwardingProxy(listen_port=args.listen_port, creds_file=args.creds_file)

    # Handle shutdown signals
    def shutdown_handler(signum: int, frame: FrameType | None) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        proxy.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        proxy.start()
        logger.info("Proxy running, press Ctrl+C to stop")
        # Keep main thread alive
        signal.pause()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, shutting down...")
        proxy.stop()
    except Exception as e:
        logger.error("Proxy failed: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

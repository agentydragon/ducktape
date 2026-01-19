"""CLI entry point for running the auth-forwarding proxy.

This script is invoked by supervisor to run the proxy as a long-running service.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from types import FrameType

from tools.claude_hooks.proxy.auth_forwarding_proxy import AuthForwardingProxy

logger = logging.getLogger(__name__)


def main() -> int:
    """Run the auth-forwarding proxy."""
    parser = argparse.ArgumentParser(description="Run auth-forwarding proxy for Bazel")
    parser.add_argument("--listen-port", type=int, required=True, help="Local port to listen on")
    parser.add_argument("--upstream-host", required=True, help="Upstream proxy host")
    parser.add_argument("--upstream-port", type=int, required=True, help="Upstream proxy port")
    parser.add_argument("--username", required=True, help="Username for upstream proxy")
    parser.add_argument("--password", required=True, help="Password/JWT for upstream proxy")
    parser.add_argument("--log-level", default="INFO", help="Logging level")

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    # Create and start proxy
    proxy = AuthForwardingProxy(
        listen_port=args.listen_port,
        upstream_host=args.upstream_host,
        upstream_port=args.upstream_port,
        username=args.username,
        password=args.password,
    )

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

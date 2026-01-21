"""HTTP server utilities for serving FastMCP servers to containers.

Provides context manager for serving FastMCP servers over Streamable HTTP transport
so containers can connect as MCP clients.
"""

from __future__ import annotations

import logging
import secrets
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastmcp.server.auth import StaticTokenVerifier

from net_util.net import pick_free_port

if TYPE_CHECKING:
    from fastmcp.server import FastMCP

logger = logging.getLogger(__name__)


@dataclass
class MCPServerHandle:
    """Handle to a running MCP HTTP server."""

    url: str
    token: str
    _stop_event: threading.Event
    _thread: threading.Thread

    def stop(self) -> None:
        """Signal the server to stop and wait for thread to exit."""
        self._stop_event.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.warning("MCP server thread did not stop cleanly within 5 seconds")


def _run_streamable_http_server(server: FastMCP, host: str, port: int, stop_event: threading.Event) -> None:
    """Run FastMCP streamable HTTP server in a thread until stop_event is set.

    FastMCP's run() blocks, so we run it in a daemon thread and use
    KeyboardInterrupt simulation via stop_event to trigger graceful shutdown.
    """
    # Override signal handler to check stop_event
    original_sigint = signal.getsignal(signal.SIGINT)

    def check_stop(*args):
        if stop_event.is_set():
            sys.exit(0)

    try:
        signal.signal(signal.SIGINT, check_stop)
        # FastMCP's run("streamable-http") will block here
        # We rely on the daemon thread being killed when main exits,
        # or on stop() being called which sets stop_event
        server.run("streamable-http", host=host, port=port)
    except SystemExit:
        pass
    finally:
        signal.signal(signal.SIGINT, original_sigint)


@contextmanager
def serve_mcp_http(server: FastMCP, *, host: str = "127.0.0.1", port: int | None = None):
    """Context manager to serve a FastMCP server over HTTP.

    Yields an MCPServerHandle with url and token for container connection.
    Server is stopped when context exits.

    Args:
        server: FastMCP server instance to serve
        host: Host to bind to (default: 127.0.0.1)
        port: Port to bind to (default: pick a free port)

    Yields:
        MCPServerHandle with url, token for MCP_SERVER_URL/MCP_SERVER_TOKEN

    Example:
        with serve_mcp_http(my_server) as handle:
            result = await run_loop_agent(
                ...,
                extra_env={"MCP_SERVER_URL": handle.url, "MCP_SERVER_TOKEN": handle.token},
            )
    """
    if port is None:
        port = pick_free_port(host)

    # Generate auth token
    token = secrets.token_urlsafe(32)

    # Configure server with auth (FastMCP uses StaticTokenVerifier for bearer token auth)
    # StaticTokenVerifier takes a dict mapping tokens to client metadata
    server.auth = StaticTokenVerifier({token: {"client_id": "mcp_client", "scopes": []}})

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_streamable_http_server, args=(server, host, port, stop_event), name="mcp-http-server", daemon=True
    )
    thread.start()

    # Wait a bit for server to start
    time.sleep(0.5)

    url = f"http://{host}:{port}"
    handle = MCPServerHandle(url=url, token=token, _stop_event=stop_event, _thread=thread)

    logger.info("MCP HTTP server started at %s", url)

    try:
        yield handle
    finally:
        logger.info("Stopping MCP HTTP server")
        handle.stop()

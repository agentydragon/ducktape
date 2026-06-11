"""Simple HTTP proxy that adds authentication to upstream proxy.

Accepts unauthenticated CONNECT requests from clients (Bazel) and forwards them
to an upstream proxy with Basic authentication added. Does NOT do TLS interception -
just tunnels the encrypted traffic through.

This is needed because Anthropic's proxy returns non-standard 401 responses
instead of 407, which breaks Java/Bazel's built-in proxy authentication.

Reads upstream proxy URL from a file on each connection, enabling credential
hot-reload without restarting the proxy.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import select
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class UpstreamConfig:
    """Upstream proxy configuration."""

    host: str
    port: int
    auth_header: str


def parse_upstream_url(url: str) -> UpstreamConfig:
    """Parse upstream proxy URL into config with auth header."""
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"Invalid upstream URL: {url}")

    host = parsed.hostname
    port = parsed.port or 80

    auth_header = ""
    if parsed.username:
        password = parsed.password or ""
        auth_str = f"{parsed.username}:{password}"
        auth_b64 = base64.b64encode(auth_str.encode()).decode()
        auth_header = f"Proxy-Authorization: Basic {auth_b64}\r\n"

    return UpstreamConfig(host=host, port=port, auth_header=auth_header)


class AuthForwardingProxy:
    """HTTP CONNECT proxy that adds authentication when forwarding to upstream.

    Workflow:
    1. Client (Bazel) sends: CONNECT example.com:443 HTTP/1.1
    2. Proxy adds auth and sends to upstream:
       CONNECT example.com:443 HTTP/1.1
       Proxy-Authorization: Basic <credentials>
    3. Upstream returns: HTTP/1.1 200 Connection Established
    4. Proxy returns: HTTP/1.1 200 Connection Established
    5. Bidirectional tunneling of encrypted data (no inspection)

    Reads upstream proxy URL from creds_file on each connection for hot-reload.
    """

    def __init__(self, listen_port: int, creds_file: Path, max_workers: int = 100):
        self.listen_port = listen_port
        self.creds_file = creds_file
        self.max_workers = max_workers
        self.server_socket: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._connections: list[socket.socket] = []
        self._conn_counter = 0
        self._conn_lock = threading.Lock()

    def _get_upstream_config(self) -> UpstreamConfig:
        """Read upstream config from creds file."""
        url = self.creds_file.read_text().strip()
        return parse_upstream_url(url)

    def start(self) -> None:
        """Start the proxy server."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("127.0.0.1", self.listen_port))
        self.server_socket.listen(50)  # Increased backlog for concurrent connections
        self.server_socket.settimeout(0.5)

        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="proxy")
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

        logger.info(
            "Auth proxy started on 127.0.0.1:%d (creds: %s, max_workers: %d)",
            self.listen_port,
            self.creds_file,
            self.max_workers,
        )

    def stop(self) -> None:
        """Stop the proxy server."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        for conn in self._connections:
            with contextlib.suppress(OSError):
                conn.close()
        if self.server_socket:
            self.server_socket.close()
        logger.info("Auth proxy stopped")

    def _serve(self) -> None:
        """Main server loop."""
        while self._running:
            try:
                client_sock, _ = self.server_socket.accept()  # type: ignore[union-attr]
                self._connections.append(client_sock)
                # Use thread pool instead of spawning unlimited threads
                self._executor.submit(self._handle_client, client_sock)  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                break

    def _handle_client(self, client_sock: socket.socket) -> None:
        """Handle a single client connection."""
        with self._conn_lock:
            self._conn_counter += 1
            conn_id = self._conn_counter

        upstream_sock: socket.socket | None = None

        try:
            # Read CONNECT request from client
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = client_sock.recv(4096)
                if not chunk:
                    return
                request += chunk

            request_str = request.decode("utf-8", errors="replace")
            lines = request_str.split("\r\n")
            request_line = lines[0]

            if not request_line.startswith("CONNECT "):
                logger.warning("Non-CONNECT request: %s", request_line)
                client_sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return

            # Parse target from CONNECT line
            parts = request_line.split()
            if len(parts) < 2:
                client_sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return

            target = parts[1]
            logger.info("[conn %d] CONNECT request for %s", conn_id, target)

            # Get upstream config (re-read from file each connection for hot-reload)
            config = self._get_upstream_config()

            # Connect to upstream proxy (30s timeout for connection only)
            logger.info("[conn %d] Connecting to upstream %s:%d", conn_id, config.host, config.port)
            upstream_sock = socket.create_connection((config.host, config.port), timeout=30)
            logger.info("[conn %d] Connected to upstream", conn_id)

            # Clear timeout - upstream may need time to connect to target before responding
            # We'll rely on the client to timeout if the whole operation takes too long
            upstream_sock.settimeout(None)

            # Forward CONNECT to upstream WITH auth header
            upstream_request = f"{request_line}\r\n"
            upstream_request += config.auth_header

            # Copy other headers from client (except Proxy-Authorization if present)
            for line in lines[1:]:
                if line and not line.lower().startswith("proxy-authorization:"):
                    upstream_request += f"{line}\r\n"

            upstream_request += "\r\n"

            upstream_sock.sendall(upstream_request.encode())

            # Read response from upstream
            upstream_response = b""
            while b"\r\n\r\n" not in upstream_response:
                chunk = upstream_sock.recv(4096)
                if not chunk:
                    logger.error("Upstream closed connection before sending response")
                    client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    return
                upstream_response += chunk

            upstream_response_str = upstream_response.decode("utf-8", errors="replace")
            logger.info("[conn %d] Upstream response: %s", conn_id, upstream_response_str.split("\r\n")[0])

            # Check if upstream accepted the connection
            if not upstream_response_str.startswith("HTTP/1.1 200"):
                logger.error("[conn %d] Upstream rejected CONNECT: %s", conn_id, upstream_response_str.split("\r\n")[0])
                # Forward upstream's rejection to client
                client_sock.sendall(upstream_response)
                return

            # Forward 200 OK to client
            logger.info("[conn %d] Sending 200 to client, starting tunnel", conn_id)
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            # Clear socket timeouts for tunnel phase (downloads can take a long time)
            client_sock.settimeout(None)
            upstream_sock.settimeout(None)

            # Now tunnel data bidirectionally (no inspection)
            self._tunnel_bidirectional(client_sock, upstream_sock)
            logger.info("[conn %d] Tunnel completed for %s", conn_id, target)

        except (OSError, ValueError) as e:
            logger.error("[conn %d] Error handling client: %s", conn_id, e)
            with contextlib.suppress(OSError):
                client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        finally:
            for sock in [client_sock, upstream_sock]:
                if sock:
                    with contextlib.suppress(OSError):
                        sock.close()
            # Remove from connections list to allow garbage collection
            with contextlib.suppress(ValueError):
                self._connections.remove(client_sock)

    def _tunnel_bidirectional(self, client_sock: socket.socket, upstream_sock: socket.socket) -> None:
        """Tunnel data bidirectionally between client and upstream."""
        sockets = [client_sock, upstream_sock]

        try:
            while True:
                readable, _, errored = select.select(sockets, [], sockets, 1.0)

                if errored:
                    break

                for sock in readable:
                    try:
                        data = sock.recv(8192)
                        if not data:
                            return  # Connection closed

                        # Forward to the other socket
                        other = upstream_sock if sock is client_sock else client_sock
                        other.sendall(data)
                    except OSError:
                        return

        except OSError:
            pass

"""Simple HTTP proxy that adds authentication to upstream proxy.

Accepts unauthenticated CONNECT requests from clients (Bazel) and forwards them
to an upstream proxy with Basic authentication added. Does NOT do TLS interception -
just tunnels the encrypted traffic through.

This is needed because Anthropic's proxy returns non-standard 401 responses
instead of 407, which breaks Java/Bazel's built-in proxy authentication.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import select
import socket
import threading

logger = logging.getLogger(__name__)


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
    """

    def __init__(self, listen_port: int, upstream_host: str, upstream_port: int, username: str, password: str):
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.username = username
        self.password = password

        # Precompute auth header
        auth_str = f"{username}:{password}"
        auth_b64 = base64.b64encode(auth_str.encode()).decode()
        self.proxy_auth_header = f"Proxy-Authorization: Basic {auth_b64}\r\n"

        self.server_socket: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._connections: list[socket.socket] = []

    def start(self) -> None:
        """Start the proxy server."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("127.0.0.1", self.listen_port))
        self.server_socket.listen(10)
        self.server_socket.settimeout(0.5)

        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        logger.info(
            "Auth forwarding proxy started on 127.0.0.1:%d -> %s:%d",
            self.listen_port,
            self.upstream_host,
            self.upstream_port,
        )

    def stop(self) -> None:
        """Stop the proxy server."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        for conn in self._connections:
            with contextlib.suppress(OSError):
                conn.close()
        if self.server_socket:
            self.server_socket.close()
        logger.info("Auth forwarding proxy stopped")

    def _serve(self) -> None:
        """Main server loop."""
        while self._running:
            try:
                client_sock, _ = self.server_socket.accept()  # type: ignore[union-attr]
                self._connections.append(client_sock)
                threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
            except TimeoutError:
                continue
            except OSError:
                break

    def _handle_client(self, client_sock: socket.socket) -> None:
        """Handle a single client connection."""
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
            logger.debug("CONNECT request for %s", target)

            # Connect to upstream proxy
            upstream_sock = socket.create_connection((self.upstream_host, self.upstream_port), timeout=30)

            # Forward CONNECT to upstream WITH auth header
            # Build new request with Proxy-Authorization added
            upstream_request = f"{request_line}\r\n"
            upstream_request += self.proxy_auth_header

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
            logger.debug("Upstream response: %s", upstream_response_str.split("\r\n")[0])

            # Check if upstream accepted the connection
            if not upstream_response_str.startswith("HTTP/1.1 200"):
                logger.error("Upstream rejected CONNECT: %s", upstream_response_str.split("\r\n")[0])
                # Forward upstream's rejection to client
                client_sock.sendall(upstream_response)
                return

            # Forward 200 OK to client
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            # Now tunnel data bidirectionally (no inspection)
            self._tunnel_bidirectional(client_sock, upstream_sock)

        except (OSError, ValueError) as e:
            logger.error("Error handling client: %s", e)
            with contextlib.suppress(OSError):
                client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        finally:
            for sock in [client_sock, upstream_sock]:
                if sock:
                    with contextlib.suppress(OSError):
                        sock.close()

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

"""TCP proxies for exposing VM services on the pod network.

The Firecracker guest runs in its own network namespace (TAP bridge).
Services inside the guest (process_api on 2024/2025) are only reachable
via the guest IP (192.0.2.2). The Firecracker API is a Unix socket.

These proxies make everything reachable from the pod IP so the manager
can talk to both Firecracker and the guest without kubectl exec.

  Pod :2024 → guest 192.0.2.2:2024  (process_api WebSocket)
  Pod :2025 → guest 192.0.2.2:2025  (process_api HTTP control)
  Pod :2026 → Firecracker Unix socket (FC management API)
"""

from __future__ import annotations

import logging
import selectors
import socket
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_BUFFER_SIZE = 65536


def start_tcp_to_unix_proxy(api_socket: Path, listen_port: int) -> threading.Thread:
    """Proxy TCP connections to a Unix domain socket."""
    thread = threading.Thread(target=_proxy_loop, args=(listen_port, lambda: _connect_unix(api_socket)), daemon=True)
    thread.start()
    logger.info("Proxy :%d → %s", listen_port, api_socket)
    return thread


def start_tcp_to_tcp_proxy(listen_port: int, target_host: str, target_port: int) -> threading.Thread:
    """Proxy TCP connections to a remote TCP address."""
    thread = threading.Thread(
        target=_proxy_loop, args=(listen_port, lambda: _connect_tcp(target_host, target_port)), daemon=True
    )
    thread.start()
    logger.info("Proxy :%d → %s:%d", listen_port, target_host, target_port)
    return thread


def _connect_unix(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(path))
    return sock


def _connect_tcp(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    return sock


def _proxy_loop(listen_port: int, connect_fn: Callable[[], socket.socket]) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Bind to all interfaces — the manager reaches these ports via the pod IP.
    server.bind(("0.0.0.0", listen_port))
    server.listen(4)

    while True:
        tcp_conn, addr = server.accept()
        # Handle each connection in its own thread so we can serve
        # concurrent WebSocket + HTTP connections.
        threading.Thread(target=_handle_connection, args=(tcp_conn, connect_fn, addr), daemon=True).start()


def _handle_connection(tcp_conn: socket.socket, connect_fn: Callable[[], socket.socket], addr: tuple) -> None:
    try:
        backend = connect_fn()
    except OSError:
        logger.warning("Proxy: backend connection failed for %s", addr)
        tcp_conn.close()
        return

    sel = selectors.DefaultSelector()
    sel.register(tcp_conn, selectors.EVENT_READ)
    sel.register(backend, selectors.EVENT_READ)

    try:
        while True:
            events = sel.select(timeout=60)
            if not events:
                continue
            for key, _ in events:
                if key.fileobj is tcp_conn:
                    data = tcp_conn.recv(_BUFFER_SIZE)
                    if not data:
                        return
                    backend.sendall(data)
                else:
                    data = backend.recv(_BUFFER_SIZE)
                    if not data:
                        return
                    tcp_conn.sendall(data)
    except OSError:
        logger.debug("Proxy connection closed for %s", addr)
    finally:
        sel.close()
        backend.close()
        tcp_conn.close()

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class Listener:
    """A bound listening TCP socket and the address assigned to it."""

    socket: socket.socket
    host: str
    port: int


def _bind_tcp_socket(host: str, *, port: int = 0, listen: bool) -> tuple[socket.socket, str, int]:
    """Bind a TCP port without reuse options; zero selects an OS-assigned port."""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    last_error: OSError | None = None
    for family, socktype, proto, _, address in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.bind(address)
            if listen:
                sock.listen()
        except OSError as exc:
            last_error = exc
            sock.close()
            continue
        bound_host, bound_port = sock.getsockname()[:2]
        return sock, str(bound_host), int(bound_port)
    if last_error is not None:
        raise last_error
    raise OSError(f"could not resolve TCP bind address {host!r}")


@contextmanager
def listen_tcp(host: str = "127.0.0.1", *, port: int = 0) -> Iterator[Listener]:
    """Bind and listen, retaining ownership until context exit; zero selects an OS-assigned port.

    The socket has no ``SO_REUSEADDR`` or ``SO_REUSEPORT`` option. The caller may hand
    ``listener.socket`` directly to a server without releasing the port in between.
    """
    sock, bound_host, bound_port = _bind_tcp_socket(host, port=port, listen=True)
    try:
        yield Listener(socket=sock, host=bound_host, port=bound_port)
    finally:
        sock.close()


@contextmanager
def refusing_tcp_endpoint(host: str = "127.0.0.1") -> Iterator[tuple[str, int]]:
    """Hold an ephemeral TCP port bound without listening, so connections are refused."""
    sock, bound_host, port = _bind_tcp_socket(host, listen=False)
    try:
        yield bound_host, port
    finally:
        sock.close()

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed


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


async def async_wait_for_port(host: str, port: int, *, timeout_secs: float = 10.0, interval_secs: float = 0.25) -> None:
    """Wait asynchronously until host:port accepts TCP connections or timeout."""

    async def _try_connect() -> None:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.5)
        writer.close()
        await writer.wait_closed()

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(timeout_secs),
            wait=wait_fixed(interval_secs),
            retry=retry_if_exception_type((OSError, TimeoutError)),
            reraise=True,
        ):
            with attempt:
                await _try_connect()
    except OSError, TimeoutError:
        raise TimeoutError(f"port did not become ready: {host}:{port}")


def reserve_free_port(host: str = "127.0.0.1") -> int:
    """An OS-assigned port briefly held for a server in another process.

    The port is left in TIME_WAIT (about a minute): until then the kernel gives it to no other bind to
    port 0 or outgoing connection. A server binding it with SO_REUSEADDR (Go's ``net.Listen``, asyncio,
    uvicorn) can still use it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, 0))
        listener.listen()
        address = listener.getsockname()
        with socket.create_connection(address):
            accepted, _ = listener.accept()
            # The end that closes first keeps the TIME_WAIT, so it stays on the server's address.
            accepted.close()
    return int(address[1])

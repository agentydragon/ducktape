from __future__ import annotations

import asyncio
import socket

from tenacity import AsyncRetrying, Retrying, retry_if_exception_type, stop_after_delay, wait_fixed


def wait_for_port(host: str, port: int, *, timeout_secs: float = 10.0, interval_secs: float = 0.25) -> None:
    """Block until host:port accepts TCP connections or timeout.

    Uses tenacity for robust retrying with configurable timeout and interval.
    """

    def _try_connect() -> None:
        with socket.create_connection((host, int(port)), 0.5):
            pass

    try:
        Retrying(
            stop=stop_after_delay(timeout_secs),
            wait=wait_fixed(interval_secs),
            retry=retry_if_exception_type(OSError),
            reraise=True,
        )(_try_connect)
    except OSError:
        raise TimeoutError(f"port did not become ready: {host}:{port}")


async def async_wait_for_port(host: str, port: int, *, timeout_secs: float = 10.0, interval_secs: float = 0.25) -> None:
    """Async version of wait_for_port. Uses tenacity AsyncRetrying."""

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


def bind_free_port(host: str = "127.0.0.1") -> socket.socket:
    """A listening socket on an OS-assigned port, to hand to the server that will accept on it.

    The port is held from the moment it's chosen, so nothing else on a busy host can take it before
    the server serves on it; read it back with ``sock.getsockname()[1]``.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen()
    return sock


def bind_refusing_port(host: str = "127.0.0.1") -> socket.socket:
    """A socket bound to an OS-assigned port that never listens: connections to the port are refused,
    and nothing else can bind it while the socket is open. For an address a test needs unreachable."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    return sock

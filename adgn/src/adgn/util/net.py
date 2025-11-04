from __future__ import annotations

import socket

from tenacity import retry, stop_after_delay, wait_fixed


@retry(stop=stop_after_delay(10), wait=wait_fixed(0.25), reraise=True)
def _try_connect(host: str, port: int) -> None:
    """Single connection attempt that raises OSError on failure."""
    with socket.create_connection((host, int(port)), 0.5):
        pass


def wait_for_port(host: str, port: int, *, timeout_secs: float = 10.0, interval_secs: float = 0.25) -> None:
    """Block until host:port accepts TCP connections or timeout.

    Uses tenacity for robust retrying with configurable timeout and interval.
    """

    @retry(stop=stop_after_delay(timeout_secs), wait=wait_fixed(interval_secs), reraise=True)
    def _attempt():
        with socket.create_connection((host, int(port)), 0.5):
            pass

    try:
        _attempt()
    except OSError as e:
        raise TimeoutError(f"port did not become ready: {host}:{port}") from e


async def await_tcp_ready(host: str, port: int, *, timeout_secs: float = 2.5, interval_secs: float = 0.05) -> None:
    """Await until a TCP connect to (host, port) succeeds.

    Uses tenacity for robust async retrying with configurable timeout and interval.
    """
    import anyio
    from tenacity import AsyncRetrying

    async for attempt in AsyncRetrying(
        stop=stop_after_delay(timeout_secs), wait=wait_fixed(interval_secs), reraise=True
    ):
        with attempt:
            stream = await anyio.connect_tcp(host, port)
            await stream.aclose()

from __future__ import annotations

import socket

from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed


def wait_for_port(host: str, port: int, *, timeout_secs: float = 10.0, interval_secs: float = 0.25) -> None:
    """Block until host:port accepts TCP connections or timeout.

    Uses monotonic time; raises TimeoutError on expiry.
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


def pick_free_port(host: str = "127.0.0.1") -> int:
    """Return an available TCP port on host by briefly binding a socket.

    Best-effort and race-tolerant for test usage.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        _, port = s.getsockname()
        return int(port)

from __future__ import annotations

import socket
from typing import Literal, overload

from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed


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


@overload
def _try_bind(host: str, port: Literal[0]) -> int: ...


@overload
def _try_bind(host: str, port: int) -> int | None: ...


def _try_bind(host: str, port: int) -> int | None:
    """Try to bind to host:port. Returns bound port on success, None on failure.

    Port 0 always succeeds (OS assigns an available port).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return None
        _, bound_port = s.getsockname()
        return int(bound_port)


def pick_free_port(host: str = "127.0.0.1", *, preferred: int | None = None, max_tries: int = 100) -> int:
    """Return an available TCP port on host by briefly binding a socket.

    If preferred is given, tries that port first, then scans upward.
    Otherwise binds to port 0 to get any available port.
    Best-effort and race-tolerant.
    """
    if preferred is None:
        return _try_bind(host, 0)

    for p in range(preferred, preferred + max_tries):
        if (bound := _try_bind(host, p)) is not None:
            return bound
    return preferred

from __future__ import annotations

import asyncio

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed


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

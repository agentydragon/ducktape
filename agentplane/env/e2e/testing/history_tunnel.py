"""A local CONNECT passthrough for Runtime's end-to-end History SVID connection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress


@asynccontextmanager
async def history_tunnel(target: str) -> AsyncIterator[str]:
    """Forward one exact CONNECT authority to History while leaving TLS end to end."""
    tasks: set[asyncio.Task[None]] = set()
    host, port = target.rsplit(":", 1)

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(10):
                headers = await reader.readuntil(b"\r\n\r\n")
            if headers.split(b"\r\n", 1)[0] != f"CONNECT {target} HTTP/1.1".encode():
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            remote, upstream = await asyncio.open_connection(host, int(port))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            forward = asyncio.create_task(pump(reader, upstream))
            backward = asyncio.create_task(pump(remote, writer))
            try:
                await asyncio.wait((forward, backward), return_when=asyncio.FIRST_COMPLETED)
            finally:
                forward.cancel()
                backward.cancel()
                await asyncio.gather(forward, backward, return_exceptions=True)
        finally:
            writer.close()
            if upstream is not None:
                upstream.close()
                with suppress(ConnectionError):
                    await upstream.wait_closed()
            with suppress(ConnectionError):
                await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(handle(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        yield f"127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

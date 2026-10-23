"""Scoped byte relays for a held TCP endpoint whose upstream cannot inherit its socket."""

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


@asynccontextmanager
async def relay_tcp(listener: socket.socket, upstream: tuple[str, int] | Path) -> AsyncIterator[None]:
    connections: set[asyncio.Task[None]] = set()
    closing = False

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if isinstance(upstream, Path):
                peer_reader, peer_writer = await asyncio.open_unix_connection(upstream)
            else:
                peer_reader, peer_writer = await asyncio.open_connection(*upstream)
            try:
                async with asyncio.TaskGroup() as copies:
                    copies.create_task(_copy(reader, peer_writer))
                    copies.create_task(_copy(peer_reader, writer))
            finally:
                peer_writer.close()
                await peer_writer.wait_closed()
        except* OSError as errors:
            # Upstream startup and deliberate nginx reloads can refuse/reset a connection.
            logger.debug("relay connection ended: %s", errors)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                logger.debug("relay client reset during close", exc_info=True)

    async with asyncio.TaskGroup() as tasks:

        def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if closing:
                writer.close()
                return
            task = tasks.create_task(relay(reader, writer))
            connections.add(task)
            task.add_done_callback(connections.discard)

        async with await asyncio.start_server(accepted, sock=listener) as server:
            try:
                yield
            finally:
                closing = True
                server.close()
                # Server.wait_closed also waits for accepted connections. Stop
                # their tasks first, so waiting cannot depend on a live peer.
                for task in connections:
                    task.cancel()
                await server.wait_closed()

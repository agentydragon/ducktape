import asyncio
from pathlib import Path

import pytest
import pytest_bazel

from util.net import listen_tcp
from util.testing.tcp_relay import relay_tcp, relay_tcp_sync


async def _answer_after_eof(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        body = await reader.read()
        writer.write(b"reply:" + body)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("unix", [False, True])
async def test_preserves_bytes_and_half_closed_request(tmp_path: Path, unix: bool) -> None:
    upstream: Path | tuple[str, int]
    if unix:
        upstream = tmp_path / "upstream.sock"
        server = await asyncio.start_unix_server(_answer_after_eof, upstream)
    else:
        server = await asyncio.start_server(_answer_after_eof, "127.0.0.1", 0)
        upstream = ("127.0.0.1", server.sockets[0].getsockname()[1])
    with listen_tcp() as listener:
        async with server, relay_tcp(listener.socket, upstream), asyncio.timeout(5):
            reader, writer = await asyncio.open_connection(listener.host, listener.port)
            try:
                writer.write(b"\x00test\xff")
                writer.write_eof()
                assert await reader.read() == b"reply:\x00test\xff"
            finally:
                writer.close()
                await writer.wait_closed()


async def test_threaded_relay_stops_existing_connections() -> None:
    connected = asyncio.Event()
    finished = asyncio.Event()

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.set()
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    async with await asyncio.start_server(upstream, "127.0.0.1", 0) as server, asyncio.timeout(5):
        with listen_tcp() as listener:
            with relay_tcp_sync(listener.socket, ("127.0.0.1", server.sockets[0].getsockname()[1])):
                reader, writer = await asyncio.open_connection(listener.host, listener.port)
                await connected.wait()
            try:
                assert await reader.read() == b""
                await finished.wait()
            finally:
                writer.close()
                await writer.wait_closed()


if __name__ == "__main__":
    pytest_bazel.main()

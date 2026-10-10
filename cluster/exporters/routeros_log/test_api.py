import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_bazel

from cluster.exporters.routeros_log.api import ApiError, Connection, encode_sentence, read_sentence

# A fake device: answers each sentence with the sentences `respond` returns.
Respond = Callable[[list[str]], list[list[str]]]
Connect = Callable[[Respond], Awaitable[Connection]]


@pytest.fixture
async def connect() -> AsyncIterator[Connect]:
    servers: list[asyncio.Server] = []

    async def start(respond: Respond) -> Connection:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while True:
                try:
                    sentence = await read_sentence(reader)
                except asyncio.IncompleteReadError:
                    return
                for reply in respond(sentence):
                    writer.write(encode_sentence(reply))
                await writer.drain()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(server)
        reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname()[:2])
        return Connection(reader, writer, timeout_seconds=5)

    yield start
    for server in servers:
        server.close()


@pytest.mark.parametrize("length", [0, 1, 0x7F, 0x80, 0x3FFF, 0x4000, 0x1FFFFF, 0x200000])
async def test_word_lengths_round_trip(length: int) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(encode_sentence(["x" * length]))
    assert await read_sentence(reader) == (["x" * length] if length else [])


async def test_command_returns_each_re_reply(connect: Connect) -> None:
    def respond(sentence: list[str]) -> list[list[str]]:
        assert sentence == ["/log/print", "=detail="]
        return [["!re", "=.id=*1", "=message=a=b"], ["!re", "=.id=*2", "=message="], ["!done"]]

    connection = await connect(respond)
    assert await connection.command("/log/print", {"detail": ""}) == [
        {".id": "*1", "message": "a=b"},
        {".id": "*2", "message": ""},
    ]


async def test_trap_raises_after_done(connect: Connect) -> None:
    connection = await connect(lambda _: [["!trap", "=message=invalid user name or password"], ["!done"]])
    with pytest.raises(ApiError, match="invalid user name"):
        await connection.command("/login", {"name": "u", "password": "p"})


async def test_empty_reply_is_no_rows(connect: Connect) -> None:
    connection = await connect(lambda _: [["!empty"], ["!done"]])
    assert await connection.command("/log/print") == []


if __name__ == "__main__":
    pytest_bazel.main()

"""A minimal client for the RouterOS API (https://help.mikrotik.com/docs/spaces/ROS/pages/47579160/API):
length-prefixed words over TLS, a sentence per command, `!re` replies until `!done`."""

from __future__ import annotations

import asyncio
import ssl


class ApiError(Exception):
    """A `!trap` or `!fatal` reply."""


def encode_length(length: int) -> bytes:
    if length < 0x80:
        return length.to_bytes(1, "big")
    if length < 0x4000:
        return (length | 0x8000).to_bytes(2, "big")
    if length < 0x200000:
        return (length | 0xC00000).to_bytes(3, "big")
    if length < 0x10000000:
        return (length | 0xE0000000).to_bytes(4, "big")
    return b"\xf0" + length.to_bytes(4, "big")


def encode_sentence(words: list[str]) -> bytes:
    return b"".join(encode_length(len(raw)) + raw for raw in (word.encode() for word in [*words, ""]))


async def _read_length(reader: asyncio.StreamReader) -> int:
    first = (await reader.readexactly(1))[0]
    if first < 0x80:
        return first
    if first < 0xC0:
        return int.from_bytes(bytes([first & 0x3F]) + await reader.readexactly(1), "big")
    if first < 0xE0:
        return int.from_bytes(bytes([first & 0x1F]) + await reader.readexactly(2), "big")
    if first < 0xF0:
        return int.from_bytes(bytes([first & 0x0F]) + await reader.readexactly(3), "big")
    return int.from_bytes(await reader.readexactly(4), "big")


async def read_sentence(reader: asyncio.StreamReader) -> list[str]:
    words = []
    while length := await _read_length(reader):
        words.append((await reader.readexactly(length)).decode(errors="replace"))
    return words


def _attributes(words: list[str]) -> dict[str, str]:
    """`=key=value` words; the value may itself contain `=`."""
    return dict(word[1:].split("=", 1) for word in words if word.startswith("="))


class Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout_seconds: float) -> None:
        self._reader = reader
        self._writer = writer
        self._timeout_seconds = timeout_seconds

    @classmethod
    async def open(
        cls, *, host: str, port: int, ssl_context: ssl.SSLContext, username: str, password: str, timeout_seconds: float
    ) -> Connection:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context), timeout=timeout_seconds
        )
        connection = cls(reader, writer, timeout_seconds)
        try:
            await connection.command("/login", {"name": username, "password": password})
        except BaseException:
            connection.abort()
            raise
        return connection

    async def command(self, path: str, attributes: dict[str, str] | None = None) -> list[dict[str, str]]:
        """Run `path`; return each `!re` reply's attributes."""
        words = [path, *(f"={key}={value}" for key, value in (attributes or {}).items())]
        self._writer.write(encode_sentence(words))
        await self._writer.drain()
        return await asyncio.wait_for(self._replies(), timeout=self._timeout_seconds)

    async def _replies(self) -> list[dict[str, str]]:
        replies = []
        error: ApiError | None = None
        while True:
            sentence = await read_sentence(self._reader)
            match sentence[0]:
                case "!re":
                    replies.append(_attributes(sentence[1:]))
                case "!trap":
                    error = ApiError(_attributes(sentence[1:]).get("message", "trap"))
                case "!fatal":
                    raise ApiError(" ".join(sentence[1:]))
                case "!done":
                    if error is not None:
                        raise error
                    return replies
                case "!empty":
                    pass
                case other:
                    raise ApiError(f"unexpected reply {other=}")

    def abort(self) -> None:
        """Drop the connection without waiting for the peer, which may be gone."""
        self._writer.transport.abort()

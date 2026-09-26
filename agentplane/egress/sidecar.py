"""The per-Pod relay of the secure egress integration: the sandbox's tools speak ordinary HTTP proxy
to this listener on loopback, and every request and every CONNECT is forwarded to the central proxy
with `Proxy-Authorization: Bearer <token>` added, the token being the Pod's projected ServiceAccount
token with the proxy's audience (agentplane/docs/adr_sandbox_proxy_gateway.md).

The sidecar holds no credential and no TLS material and never looks inside a tunnel: a CONNECT is
sent on with the header, the central proxy's status line is relayed back, and on success bytes are
piped both ways. A plain request is forwarded with the header and `Connection: close` — one client
request per relayed connection — and its response streamed back. The token is read from the
projected file on every request because kubelet rotates it in place; no token, or no central proxy,
is a refusal from the sidecar, never a request without the header.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Self

from aiohttp import web
from more_itertools import one
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

REFUSED_HEADER = "x-agentplane-egress-sidecar"
_MAX_HEAD_BYTES = 64 * 1024
_PIPE_CHUNK = 64 * 1024
PROJECTED_TOKEN_HEADER = "X-Agentplane-Workload-Token"
READINESS_PATH = "/readyz"
READINESS_HOST = "0.0.0.0"
READINESS_PORT = 3129
# Never forwarded from the client: both token headers are the sidecar's alone to set, and
# Proxy-Connection is the pre-standard keep-alive hint some clients send a proxy. The workload's
# own headers pass through this relay, so dropping the projected one here is what makes its
# presence upstream mean the sidecar put it there -- the central proxy reviews it as well, and
# neither check is the other's excuse.
_DROPPED_HEADERS = frozenset({b"proxy-authorization", b"proxy-connection", PROJECTED_TOKEN_HEADER.lower().encode()})


class RefusalReason(StrEnum):
    BAD_REQUEST = "bad-request"
    TOKEN_UNAVAILABLE = "token-unavailable"
    PROXY_UNREACHABLE = "proxy-unreachable"


def _refusal(status: int, phrase: str, reason: RefusalReason) -> bytes:
    return (
        f"HTTP/1.1 {status} {phrase}\r\n{REFUSED_HEADER}: reason={reason}\r\n"
        "Content-Length: 0\r\nConnection: close\r\n\r\n"
    ).encode()


def _read_token_file(path: Path) -> str | None:
    """A projected token, or None when its file is missing, unreadable, or empty."""
    try:
        token = path.read_text().strip()
    except OSError:
        logger.exception("token file %s unreadable", path)
        return None
    if not token:
        logger.error("token file %s is empty", path)
    return token or None


def _is_connect(head: bytes) -> bool:
    """Whether the request head opens a tunnel. Raises ValueError on a head that is not an HTTP/1.x request."""
    request_line = head.split(b"\r\n", 1)[0].split(b" ")
    if len(request_line) != 3 or not request_line[2].startswith(b"HTTP/1."):
        raise ValueError(f"not an HTTP/1.x request line: {request_line!r}")
    return request_line[0] == b"CONNECT"


def _rewrite_head(head: bytes, *, token: str, projected: Mapping[str, str], is_connect: bool) -> bytes:
    """The request head to send the central proxy."""
    lines = head[:-4].split(b"\r\n")
    kept = [line for line in lines[1:] if line.split(b":", 1)[0].strip().lower() not in _DROPPED_HEADERS]
    if not is_connect:
        kept = [line for line in kept if line.split(b":", 1)[0].strip().lower() != b"connection"]
        kept.append(b"Connection: close")
    kept.append(b"Proxy-Authorization: Bearer " + token.encode())
    # On a CONNECT too, and above all there: the requests inside the tunnel are behind its TLS, so
    # what the CONNECT presents is the only thing the central proxy can substitute into them.
    kept.extend(
        f"{PROJECTED_TOKEN_HEADER}: {audience} {value}".encode() for audience, value in sorted(projected.items())
    )
    return b"\r\n".join([lines[0], *kept]) + b"\r\n\r\n"


def _status_of(response_head: bytes) -> int:
    parts = response_head.split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/1."):
        raise ValueError(f"not an HTTP/1.x status line: {response_head[:64]!r}")
    return int(parts[1])


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy until EOF, then half-close the far side so its reader sees the EOF too."""
    try:
        while data := await reader.read(_PIPE_CHUNK):
            writer.write(data)
            await writer.drain()
    except ConnectionError, asyncio.IncompleteReadError:
        pass
    finally:
        if writer.can_write_eof() and not writer.is_closing():
            with contextlib.suppress(OSError):
                writer.write_eof()


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


class SidecarRelay:
    """The loopback listener. Async context manager: entering binds it (`listen_port=0` picks an
    ephemeral port, then read `listen_port`); exiting stops it and drops every open relay."""

    def __init__(
        self,
        *,
        proxy_host: str,
        proxy_port: int,
        token_file: Path,
        audience_token_files: Mapping[str, Path] = MappingProxyType({}),
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
    ) -> None:
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._token_file = token_file
        self._audience_token_files = audience_token_files
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._server: asyncio.Server | None = None
        self._relays: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(
            self._accept, self._listen_host, self._listen_port, limit=_MAX_HEAD_BYTES
        )
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        assert self._server is not None
        self._server.close()
        for relay in self._relays:
            relay.cancel()
        await asyncio.gather(*self._relays, return_exceptions=True)
        await self._server.wait_closed()

    @property
    def listen_port(self) -> int:
        if self._server is None:
            raise RuntimeError("sidecar relay is not running")
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def is_ready(self) -> bool:
        """Whether the local listener that accepts workload proxy traffic is serving."""
        return self._server is not None and self._server.is_serving()

    def _read_token(self) -> str | None:
        """The current hop token, or None when the projected file is missing, unreadable, or empty."""
        return _read_token_file(self._token_file)

    def _read_projected(self) -> dict[str, str]:
        """The audience-scoped tokens to offer the central proxy, re-read like the hop token.

        An audience whose file is missing or empty is left out rather than offered blank: the rule
        naming it then denies at the point of use, where a blank would look like a rejected token.
        """
        return {
            audience: token
            for audience, path in self._audience_token_files.items()
            if (token := _read_token_file(path)) is not None
        }

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._relays.add(task)
        try:
            await self._relay(reader, writer)
        finally:
            self._relays.discard(task)
            await _close(writer)

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            is_connect = _is_connect(head)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as error:
            logger.warning("refusing malformed request: %s", error)
            writer.write(_refusal(400, "Bad Request", RefusalReason.BAD_REQUEST))
            return
        token = self._read_token()
        if token is None:
            writer.write(_refusal(503, "Service Unavailable", RefusalReason.TOKEN_UNAVAILABLE))
            return
        forwarded = _rewrite_head(head, token=token, projected=self._read_projected(), is_connect=is_connect)
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(self._proxy_host, self._proxy_port)
        except OSError as error:
            logger.warning("central proxy %s:%d unreachable: %s", self._proxy_host, self._proxy_port, error)
            writer.write(_refusal(502, "Bad Gateway", RefusalReason.PROXY_UNREACHABLE))
            return
        try:
            upstream_writer.write(forwarded)
            await upstream_writer.drain()
            if is_connect:
                # The tunnel exists only once the central proxy says so; until then its answer is
                # relayed verbatim, and a refusal ends the relay with nothing piped.
                response_head = await upstream_reader.readuntil(b"\r\n\r\n")
                writer.write(response_head)
                await writer.drain()
                if not 200 <= _status_of(response_head) < 300:
                    await _pipe(upstream_reader, writer)
                    return
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, ConnectionError) as error:
            logger.warning("relay to %s:%d ended: %s", self._proxy_host, self._proxy_port, error)
        finally:
            await _close(upstream_writer)


_RELAY: web.AppKey[SidecarRelay] = web.AppKey("relay", SidecarRelay)


async def _readyz(request: web.Request) -> web.Response:
    ready = request.app[_RELAY].is_ready
    return web.json_response({"ready": ready}, status=200 if ready else 503)


@asynccontextmanager
async def serve_readiness(relay: SidecarRelay, *, host: str, port: int) -> AsyncIterator[int]:
    """Serve a non-sensitive kubelet endpoint separate from the loopback proxy listener."""
    app = web.Application()
    app[_RELAY] = relay
    app.router.add_get(READINESS_PATH, _readyz)
    runner = web.AppRunner(app, shutdown_timeout=5)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
        yield int(one(runner.addresses)[1])
    finally:
        await runner.cleanup()


class Settings(BaseSettings):
    """Each field is a `--flag` and an `AGENTPLANE_EGRESS_SIDECAR_*` environment variable."""

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_EGRESS_SIDECAR_", cli_parse_args=True, cli_kebab_case=True)

    proxy_host: str = Field(description="Host of the central egress proxy every request is relayed to.")
    proxy_port: int = Field(default=8888, description="The central proxy's listener port.")
    token_file: Path = Field(description="The projected ServiceAccount token file, re-read on every request.")
    audience_token_files: dict[str, Path] = Field(
        default_factory=dict,
        description="Audience to the projected token file holding this Pod's identity for it, offered to the "
        "central proxy to substitute where a rule names that audience. The hop token above is not one of "
        "these: it carries the proxy's own audience, which a destination validating its own refuses.",
    )
    listen_host: str = Field(default="127.0.0.1", description="Bind address; loopback, the runner's own Pod.")
    listen_port: int = Field(default=3128, description="Port the runner container's HTTP(S)_PROXY names.")
    readiness_host: str = Field(default=READINESS_HOST, description="Bind address for the kubelet readiness endpoint.")
    readiness_port: int = Field(default=READINESS_PORT, description="HTTP port serving /readyz.")

    def __init__(self, **values: Any) -> None:
        # BaseSettings fills required fields from its sources; spell that out because the mypy plugin
        # derives a required-argument signature from the fields.
        super().__init__(**values)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(Settings()))


async def async_main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    relay = SidecarRelay(
        proxy_host=settings.proxy_host,
        proxy_port=settings.proxy_port,
        token_file=settings.token_file,
        audience_token_files=settings.audience_token_files,
        listen_host=settings.listen_host,
        listen_port=settings.listen_port,
    )
    async with (
        serve_readiness(relay, host=settings.readiness_host, port=settings.readiness_port) as readiness_port,
        relay,
    ):
        logger.info(
            "relaying %s:%d -> %s:%d; readiness on %s:%d%s",
            settings.listen_host,
            relay.listen_port,
            settings.proxy_host,
            settings.proxy_port,
            settings.readiness_host,
            readiness_port,
            READINESS_PATH,
        )
        await stop.wait()


if __name__ == "__main__":
    main()

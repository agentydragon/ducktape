"""The console's ICAP REQMOD listener.

Squid's egress fence points ``icap_service … bypass=0`` here and asks, per request, what to do with
it. The console answers with a policy decision *and* the credential the agent should have used —
see <cluster/docs/plans/agent_egress_proxy_options.md> for why substitution lives here rather than
in the proxy.

This module owns connections and framing only. What a request may do, and which credential it earns,
is an ``ReqmodAdapter`` supplied by the caller — so the protocol can be exercised against a fake in
tests without standing up console's policy or database.

**A new inbound surface.** Everything else console listens on is HTTP behind uvicorn. ICAP is not
HTTP and cannot be a FastAPI route, so this is a plain ``asyncio.start_server`` started from the app
lifespan. Whatever reaches this port can ask the console to hand it a credential, so it must be
reachable only from the fence proxies — enforced by NetworkPolicy, not by anything here.

**Failures must stay loud.** With ``bypass=0`` Squid renders a dropped or malformed ICAP
transaction as ``ERR_ICAP_FAILURE`` and refuses the request, which is the fail-closed behaviour the
fence depends on. So an adapter that raises drops the connection rather than forwarding: the
measured alternative — Squid forwarding the agent's unresolved placeholder to the origin — is the
failure this design exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from haku.console.icap_protocol import (
    CONTINUE_100,
    Adaptation,
    IcapProtocolError,
    OptionsAnnouncement,
    OptionsRequest,
    ReqmodRequest,
    read_preview_remainder,
    read_request,
    serialise_adaptation,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 1344


class ReqmodAdapter(Protocol):
    """Decides what happens to one request, and rewrites it if it is allowed to proceed."""

    async def adapt(self, request: ReqmodRequest) -> Adaptation: ...

    @property
    def announcement(self) -> OptionsAnnouncement:
        """What to tell Squid in reply to OPTIONS.

        A property rather than a constant because its ``istag`` must change whenever the adapter's
        behaviour does — Squid keys cached adaptations on it, so a fixed tag across a policy change
        means old decisions outliving the policy that produced them.
        """
        ...


class IcapServer:
    def __init__(self, adapter: ReqmodAdapter, *, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
        self._adapter = adapter
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """The bound port, which differs from the requested one when that was 0 (tests)."""
        if self._server is None:
            return self._port
        # getsockname() is typed Any; for an AF_INET socket the address is (host, port).
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, self._host, self._port)
        logger.info("ICAP REQMOD listening on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while await self._transaction(reader, writer):
                pass
        except (IcapProtocolError, asyncio.IncompleteReadError):
            # Dropping the connection is the signal. Squid turns it into ERR_ICAP_FAILURE and denies
            # the request, so this logs at warning rather than error: the fence held.
            logger.warning("ICAP transaction from %s dropped", peer, exc_info=True)
        except ConnectionResetError:
            logger.info("ICAP peer %s went away", peer)
        finally:
            writer.close()

    async def _transaction(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """Handle one transaction. ``False`` once the peer has stopped sending."""
        request = await read_request(reader)
        if request is None:
            return False

        if isinstance(request, OptionsRequest):
            writer.write(self._adapter.announcement.serialise())
            await writer.drain()
            return True

        if not request.body_complete:
            writer.write(CONTINUE_100.payload)
            await writer.drain()
            request = await read_preview_remainder(reader, request)

        adaptation = await self._adapter.adapt(request)
        response = serialise_adaptation(adaptation, request, istag=self._adapter.announcement.istag)
        writer.write(response.payload)
        await writer.drain()
        return response.keep_alive

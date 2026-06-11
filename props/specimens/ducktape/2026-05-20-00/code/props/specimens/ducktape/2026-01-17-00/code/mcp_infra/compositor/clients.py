from __future__ import annotations

from typing import cast

from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport

from mcp_infra.compositor.server import Compositor
from mcp_infra.snapshots import ServerEntry


class CompositorMetaClient:
    """Typed client for compositor_meta read-only resources (per-server state).

    Provides list_states() to read the current server entries via resource URIs.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @property
    def client(self) -> Client:
        return self._client

    async def list_states(self) -> dict[str, ServerEntry]:
        if not (
            isinstance(transport := self._client.transport, FastMCPTransport)
            and isinstance(comp := transport.server, Compositor)
        ):
            raise RuntimeError("CompositorMetaClient requires an in-process Compositor transport")
        return cast(dict[str, ServerEntry], await comp.server_entries())


# Internal helper; prefer explicit imports

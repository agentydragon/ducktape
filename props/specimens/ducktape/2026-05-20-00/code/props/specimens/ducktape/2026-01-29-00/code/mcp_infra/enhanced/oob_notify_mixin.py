"""Out-of-band notifications mixin for FastMCP.

Provides session capturing and broadcast methods for protocol notifications.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from weakref import WeakSet

from fastmcp.server import FastMCP
from fastmcp.server.auth import AuthProvider
from mcp.server.session import ServerSession
from pydantic.networks import AnyUrl

from mcp_infra.urls import ANY_URL

logger = logging.getLogger(__name__)


class NotificationsMixin(FastMCP):
    """Mixin that provides out-of-band notification broadcasts.

    Captures live ServerSession objects and provides broadcast methods for:
    - broadcast_resource_updated(uri)
    - broadcast_resource_list_changed()

    Queues notifications when no sessions exist yet (flushed on first session).
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        instructions: str | None = None,
        lifespan: Callable[[FastMCP], AbstractAsyncContextManager[object]] | None = None,
        auth: AuthProvider | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, instructions=instructions, lifespan=lifespan, auth=auth, **kwargs)
        self._sessions: WeakSet[ServerSession] = WeakSet()
        self._pending_uris: list[str] = []
        self._pending_list_changed: bool = False

    async def broadcast_resource_updated(self, uri: AnyUrl | str) -> None:
        """Broadcast ResourceUpdatedNotification to all sessions."""
        # If no sessions yet, queue and return
        if not self._sessions:
            self._pending_uris.append(str(uri))
            return
        sessions = [s for s in self._sessions if s is not None]
        # Send to all current sessions; prune failures
        logger.debug("broadcast_resource_updated: uri=%s sessions=%d", uri, len(sessions))
        uri_value = uri if isinstance(uri, AnyUrl) else ANY_URL.validate_python(uri)
        results = await asyncio.gather(*[s.send_resource_updated(uri_value) for s in sessions], return_exceptions=True)
        logger.debug("broadcast done: results=%s", [repr(r) for r in results])
        # Best-effort: drop sessions that errored
        for s, r in zip(sessions, results, strict=False):
            if isinstance(r, Exception):
                logger.warning("send_resource_updated failed: %s", r)
                self._sessions.discard(s)

    async def flush_pending(self) -> None:
        """Send any queued URIs to current sessions (if any)."""
        if not self._sessions:
            return
        sessions = [s for s in self._sessions if s is not None]
        uris = self._pending_uris[:]
        self._pending_uris.clear()
        tasks: list[Awaitable[Any]] = [
            s.send_resource_updated(ANY_URL.validate_python(uri)) for s in sessions for uri in uris
        ]
        if self._pending_list_changed:
            self._pending_list_changed = False
            tasks.extend(s.send_resource_list_changed() for s in sessions)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def broadcast_resource_list_changed(self) -> None:
        """Notify clients that the server's resource list changed."""
        sessions = [s for s in list(self._sessions) if s is not None]
        if not sessions:
            self._pending_list_changed = True
            return
        logger.debug("broadcast_resource_list_changed: sessions=%d", len(sessions))
        results = await asyncio.gather(*[s.send_resource_list_changed() for s in sessions], return_exceptions=True)
        for s, r in zip(sessions, results, strict=False):
            if isinstance(r, Exception):
                logger.warning("send_resource_list_changed failed: %s", r)
                self._sessions.discard(s)

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
import logging
from typing import Any, cast
from weakref import WeakSet

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastmcp.server import FastMCP
from fastmcp.server.low_level import LowLevelServer
from mcp.server.lowlevel.server import InitializationOptions, NotificationOptions
from mcp.server.session import ServerSession
from mcp.shared.message import SessionMessage
from pydantic import BaseModel

from adgn.mcp._shared.fastmcp_flat import FlatModelToolMixin
from adgn.mcp._shared.urls import ANY_URL

logger = logging.getLogger(__name__)


class _CapturingServer(LowLevelServer):
    """Low-level Server that calls a hook when a ServerSession is created.

    This lets NotifyingFastMCP register the session as soon as initialize completes,
    so protocol notifications can be emitted before any request arrives.
    """

    def __init__(
        self,
        fastmcp: FastMCP,  # Required positional arg in fastmcp 2.13+
        *a,
        on_session_created: Callable[[ServerSession], None] | None = None,
        on_session_created_async: Callable[[ServerSession], Awaitable[None]] | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        **kw,
    ):
        super().__init__(fastmcp, *a, **kw)
        self._on_session_created = on_session_created
        self._on_session_created_async = on_session_created_async
        self._experimental_capabilities = experimental_capabilities or {}

    async def run(
        self,
        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
        write_stream: MemoryObjectSendStream[SessionMessage],
        initialization_options: InitializationOptions,
        raise_exceptions: bool = False,
        stateless: bool = False,
    ):
        name = getattr(self, "name", "<unknown>")
        logger.debug("_CapturingServer.run: start name=%s", name)
        # Intercept the moment the session is created, then handle messages in child tasks
        async with AsyncExitStack() as stack:
            lifespan_context = await stack.enter_async_context(self.lifespan(self))
            session = await stack.enter_async_context(
                ServerSession(read_stream, write_stream, initialization_options, stateless=stateless)
            )
            logger.debug("_CapturingServer.run: session created; awaiting incoming messages")
            if self._on_session_created:
                self._on_session_created(session)
            if self._on_session_created_async:
                await self._on_session_created_async(session)
            async with anyio.create_task_group() as tg:
                async for message in session.incoming_messages:

                    async def _serve(msg):
                        try:
                            await self._handle_message(msg, session, lifespan_context, raise_exceptions)
                        except BaseException as exc:  # do not cancel siblings
                            logger.exception("Server responder error: %s", exc)

                    tg.start_soon(_serve, message)

    # Merge experimental capabilities for initialization
    def create_initialization_options(
        self,
        notification_options=None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        **kwargs: Any,
    ):
        caps = dict(experimental_capabilities or {})
        # Shallow merge per capability group
        for group, values in (self._experimental_capabilities or {}).items():
            merged = dict(caps.get(group) or {})
            merged.update(values or {})
            caps[group] = merged
        return super().create_initialization_options(
            notification_options=notification_options, experimental_capabilities=caps, **kwargs
        )

    # Ensure standard capabilities reflect subscribe support when handlers exist
    def get_capabilities(
        self, notification_options: NotificationOptions, experimental_capabilities: dict[str, dict[str, Any]]
    ):
        from mcp import types as mcp_types

        caps = super().get_capabilities(notification_options, experimental_capabilities)
        # Advertise resources.subscribe if a subscribe handler is registered
        if mcp_types.SubscribeRequest in self.request_handlers:
            if caps.resources is None:
                caps.resources = mcp_types.ResourcesCapability()
            caps.resources.subscribe = True
        return caps


class NotifyingFastMCP(FlatModelToolMixin, FastMCP):
    """FastMCP subclass that can broadcast protocol notifications outside requests.

    - Captures live ServerSession objects as soon as initialize completes
    - Provides broadcast_resource_updated(uri) to emit ResourceUpdatedNotification to all sessions
    - Queues URIs when no sessions exist yet (flushed on first broadcast once a session is present)
    - Uses FastMCP's LowLevelServer to hook session creation cleanly
    """

    _mcp_server: _CapturingServer

    def __init__(
        self,
        name: str,
        *,
        instructions: str | None = None,
        lifespan: Callable[[FastMCP], AbstractAsyncContextManager[Any]] | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(name=name, instructions=instructions, lifespan=lifespan)
        self._sessions: WeakSet[ServerSession] = WeakSet()
        self._pending_uris: list[str] = []
        self._pending_list_changed: bool = False
        self._experimental_capabilities = experimental_capabilities or {}
        # Replace the low-level server with a capturing variant and re-register handlers
        prev_lifespan = self._mcp_server.lifespan

        async def _on_created(sess: ServerSession) -> None:
            # Register and flush any queued notifications
            self._sessions.add(sess)
            await self.flush_pending()

        # Wrap in a small adapter because low-level server expects a sync callable
        def _adapter(sess: ServerSession) -> None:
            # Worst-case: ensure the captured session is the correct low-level type
            assert isinstance(sess, ServerSession)

        capturing_server = _CapturingServer(
            self,  # fastmcp positional argument (required in fastmcp 2.13+)
            name=self.name,
            instructions=self.instructions,
            on_session_created=_adapter,
            on_session_created_async=_on_created,
            lifespan=prev_lifespan,
            experimental_capabilities=self._experimental_capabilities,
        )
        # Replace low-level server with our capturing variant
        self._mcp_server = capturing_server
        # Re-install FastMCP handlers on the new low-level server
        self._setup_handlers()

    # ---- Broadcast API (can be called outside request scope) ----
    async def broadcast_resource_updated(self, uri: str) -> None:
        # If no sessions yet, queue and return
        sessions = [s for s in list(self._sessions) if s is not None]
        if not sessions:
            self._pending_uris.append(uri)
            return
        # Send to all current sessions; prune failures

        logger.debug("broadcast_resource_updated: uri=%s sessions=%d", uri, len(sessions))
        uri_value = ANY_URL.validate_python(uri)
        send_tasks = [s.send_resource_updated(uri_value) for s in sessions]
        results = await asyncio.gather(*send_tasks, return_exceptions=True)
        logger.debug("broadcast done: results=%s", [repr(r) for r in results])
        # Best-effort: drop sessions that errored
        for s, r in zip(sessions, results, strict=False):
            if isinstance(r, Exception):
                logger.warning("send_resource_updated failed: %s", r)
                self._sessions.discard(s)

    async def flush_pending(self) -> None:
        """Send any queued URIs to current sessions (if any)."""
        if not self._pending_uris:
            # fall through to possibly flush list_changed
            pass
        sessions = [s for s in list(self._sessions) if s is not None]
        if not sessions:
            return
        uris = self._pending_uris[:]
        self._pending_uris.clear()
        tasks: list[Awaitable[Any]] = []
        if uris:
            tasks.extend(s.send_resource_updated(ANY_URL.validate_python(uri)) for s in sessions for uri in uris)
        if self._pending_list_changed:
            self._pending_list_changed = False
            tasks.extend(s.send_resource_list_changed() for s in sessions)
        if tasks:
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

    # ---- Convenience: flat-model decorator as a member method -------------
    def flat_model(
        self,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: Any | None = None,
        structured_output: bool = True,
        output_model: type[BaseModel] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Member-style flat model tool registration.

        Equivalent to using @self.tool(..., flat=True, flat_output_model=...). Provided
        for discoverability; prefer @self.tool(flat=True) where comfortable.
        """
        return cast(
            Callable[[Callable[..., Any]], Callable[..., Any]],
            self.tool(
                name=name,
                title=title,
                description=description,
                annotations=annotations,
                structured_output=structured_output,
                flat=True,
                flat_output_model=output_model,
            ),
        )

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastmcp.server import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.low_level import LowLevelServer
from mcp import types as mcp_types
from mcp.server.lowlevel.server import InitializationOptions, NotificationOptions
from mcp.server.session import ServerSession
from mcp.shared.message import SessionMessage

from mcp_infra.enhanced.flat_mixin import FlatModelMixin
from mcp_infra.enhanced.oob_notify_mixin import NotificationsMixin
from mcp_infra.enhanced.openai_strict_mixin import OpenAIStrictModeMixin

logger = logging.getLogger(__name__)


class _CapturingServer(LowLevelServer):
    """Low-level Server that calls a hook when a ServerSession is created.

    This lets EnhancedFastMCP register the session as soon as initialize completes,
    so protocol notifications can be emitted before any request arrives.
    """

    def __init__(
        self,
        fastmcp: FastMCP,  # Required positional arg in fastmcp 2.13+
        *a,
        on_session_created: Callable[[ServerSession], None] | None = None,
        on_session_created_async: (Callable[[ServerSession], Awaitable[None]] | None) = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        version: str | None = None,
        **kw,
    ):
        super().__init__(fastmcp, *a, version=version, **kw)
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
        logger.debug("_CapturingServer.run: start name=%s", self.name)
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
        caps = super().get_capabilities(notification_options, experimental_capabilities)
        # Advertise resources.subscribe if a subscribe handler is registered
        if mcp_types.SubscribeRequest in self.request_handlers:
            if caps.resources is None:
                caps.resources = mcp_types.ResourcesCapability()
            caps.resources.subscribe = True
        return caps


class EnhancedFastMCP(OpenAIStrictModeMixin, FlatModelMixin, NotificationsMixin, FastMCP):
    """Batteries-included FastMCP composed from 3 mixins.

    Composition:
    - OpenAIStrictModeMixin: Validates tool schemas at registration time
    - FlatModelMixin: ValidationError formatting + .flat_model() convenience
    - NotificationsMixin: Out-of-band broadcast methods
    - Plus: Session capturing via _CapturingServer
    - Plus: Experimental capabilities support

    Features:
    - Session capturing & out-of-band notification broadcasts
    - Structured ValidationError formatting (for flat-model tools)
    - OpenAI strict mode schema validation (unconditional)
    - Auto-advertise subscribe capability
    - Experimental capabilities support
    - .flat_model() convenience method
    """

    _mcp_server: LowLevelServer

    def __init__(
        self,
        name: str | None = None,
        *,
        instructions: str | None = None,
        lifespan: Callable[[FastMCP], AbstractAsyncContextManager[object]] | None = None,
        experimental_capabilities: dict[str, dict[str, object]] | None = None,
        auth: AuthProvider | None = None,
        version: str | None = None,
    ) -> None:
        # NotificationsMixin sets up _sessions, _pending_uris, _pending_list_changed
        super().__init__(name=name, instructions=instructions, lifespan=lifespan, auth=auth, version=version)
        self._experimental_capabilities = experimental_capabilities or {}

        # Replace the low-level server with a capturing variant and re-register handlers
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
            lifespan=self._mcp_server.lifespan,
            experimental_capabilities=self._experimental_capabilities,
            version=version,
        )
        # Replace low-level server with our capturing variant
        self._mcp_server = capturing_server
        # Re-install FastMCP handlers on the new low-level server
        self._setup_handlers()

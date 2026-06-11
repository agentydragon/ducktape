from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastmcp.server import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from mcp import types as mcp_types
from mcp.server.session import ServerSession

from mcp_infra.enhanced.flat_mixin import FlatModelMixin
from mcp_infra.enhanced.oob_notify_mixin import NotificationsMixin
from mcp_infra.enhanced.openai_strict_mixin import OpenAIStrictModeMixin

logger = logging.getLogger(__name__)


class _SessionCapturingMiddleware(Middleware):
    """Middleware that captures ServerSession on initialization.

    Uses v3's on_initialize hook to register sessions for out-of-band notifications.
    """

    def __init__(self, enhanced: EnhancedFastMCP) -> None:
        self._enhanced = enhanced

    async def on_initialize(
        self,
        context: MiddlewareContext[mcp_types.InitializeRequest],
        call_next: CallNext[mcp_types.InitializeRequest, mcp_types.InitializeResult | None],
    ) -> mcp_types.InitializeResult | None:
        result = await call_next(context)
        # Capture the session after successful initialization
        if context.fastmcp_context is not None and context.fastmcp_context.session is not None:
            session = context.fastmcp_context.session
            if isinstance(session, ServerSession):
                self._enhanced._sessions.add(session)
                await self._enhanced.flush_pending()
        return result


class EnhancedFastMCP(OpenAIStrictModeMixin, FlatModelMixin, NotificationsMixin, FastMCP):
    """Batteries-included FastMCP composed from 3 mixins.

    Composition:
    - OpenAIStrictModeMixin: Validates tool schemas at registration time
    - FlatModelMixin: ValidationError formatting + .flat_model() convenience
    - NotificationsMixin: Out-of-band broadcast methods
    - Plus: Session capturing via middleware (v3 on_initialize hook)
    - Plus: Auto-advertise resources.subscribe when a handler is registered
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        instructions: str | None = None,
        lifespan: Callable[[FastMCP], AbstractAsyncContextManager[object]] | None = None,
        auth: AuthProvider | None = None,
        version: str | None = None,
    ) -> None:
        super().__init__(name=name, instructions=instructions, lifespan=lifespan, auth=auth, version=version)

        self.middleware.append(_SessionCapturingMiddleware(self))

        # Patch get_capabilities to auto-advertise resources.subscribe
        # when a subscribe handler is registered. fastmcp v3 doesn't do this natively.
        mcp_server = self._mcp_server
        _base_get_caps = mcp_server.get_capabilities

        def _patched_get_capabilities(*args: Any, **kwargs: Any) -> mcp_types.ServerCapabilities:
            caps = _base_get_caps(*args, **kwargs)
            if mcp_types.SubscribeRequest in mcp_server.request_handlers:
                if caps.resources is None:
                    caps.resources = mcp_types.ResourcesCapability()
                caps.resources.subscribe = True
            return caps

        mcp_server.get_capabilities = _patched_get_capabilities  # type: ignore[method-assign]

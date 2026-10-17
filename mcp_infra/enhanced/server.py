from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastmcp.server import FastMCP
from fastmcp.server.auth import AuthProvider

from mcp_infra.enhanced.openai_strict_mixin import OpenAIStrictModeMixin

logger = logging.getLogger(__name__)


class EnhancedFastMCP(OpenAIStrictModeMixin, FastMCP):
    """FastMCP with OpenAI strict-mode schema validation.

    Tool handlers use FastMCP's native function signature and dependency injection.
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

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastmcp.server import FastMCP
from fastmcp.server.auth import AuthProvider

from mcp_infra.enhanced.flat_mixin import FlatModelMixin
from mcp_infra.enhanced.openai_strict_mixin import OpenAIStrictModeMixin

logger = logging.getLogger(__name__)


class EnhancedFastMCP(OpenAIStrictModeMixin, FlatModelMixin, FastMCP):
    """Batteries-included FastMCP composed from 2 mixins.

    Composition:
    - OpenAIStrictModeMixin: Validates tool schemas at registration time
    - FlatModelMixin: ValidationError formatting + .flat_model() convenience
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

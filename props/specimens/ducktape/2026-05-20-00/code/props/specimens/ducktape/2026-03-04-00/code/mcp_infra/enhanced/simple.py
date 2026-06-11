"""SimpleFastMCP: FastMCP with flat-model support but no notifications.

This module is separate from server.py to avoid importing _CapturingServer
and LowLevelServer, which require fastmcp >=2.13.
"""

from __future__ import annotations

from fastmcp.server import FastMCP

from mcp_infra.enhanced.flat_mixin import FlatModelMixin
from mcp_infra.enhanced.openai_strict_mixin import OpenAIStrictModeMixin


class SimpleFastMCP(OpenAIStrictModeMixin, FlatModelMixin, FastMCP):
    """FastMCP with flat-model and OpenAI strict mode validation, but no notifications.

    Use this when you need flat_model() convenience but don't need out-of-band notifications.
    This avoids the _CapturingServer which requires fastmcp >=2.13.
    """

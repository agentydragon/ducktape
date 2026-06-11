"""OpenAI strict mode validation mixin for FastMCP.

This mixin adds validation of tool input schemas against OpenAI's strict mode requirements
at tool registration time (immediately when add_tool() is called).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastmcp.server import FastMCP
from fastmcp.tools.tool import Tool

from openai_utils.pydantic_strict_mode import validate_openai_strict_mode_schema

logger = logging.getLogger(__name__)


class OpenAIStrictModeMixin(FastMCP):
    """Mixin that validates tool schemas conform to OpenAI strict mode at registration time."""

    def add_tool(self, tool: Tool | Callable[..., Any]) -> Tool:
        """Override to validate tool schema immediately at registration time.

        Validates the tool's input schema against OpenAI strict mode requirements
        before delegating to the parent add_tool() method.
        """
        # Delegate to parent first so Callable gets converted to Tool
        added_tool = super().add_tool(tool)

        # Validate schema after adding (tool is now guaranteed to be a Tool)
        validate_openai_strict_mode_schema(added_tool.parameters, model_name=added_tool.name)

        return added_tool

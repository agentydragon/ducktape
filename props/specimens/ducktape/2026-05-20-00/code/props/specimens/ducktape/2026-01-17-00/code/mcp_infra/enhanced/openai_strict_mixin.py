"""OpenAI strict mode validation mixin for FastMCP.

This mixin adds validation of tool input schemas against OpenAI's strict mode requirements
at tool registration time (immediately when add_tool() is called).
"""

from __future__ import annotations

import logging

from fastmcp.server import FastMCP
from fastmcp.tools.tool import Tool

from openai_utils.pydantic_strict_mode import validate_openai_strict_mode_schema

logger = logging.getLogger(__name__)


class OpenAIStrictModeMixin(FastMCP):
    """Mixin that validates tool schemas conform to OpenAI strict mode at registration time."""

    def add_tool(self, tool: Tool) -> Tool:
        """Override to validate tool schema immediately at registration time.

        Validates the tool's input schema against OpenAI strict mode requirements
        before delegating to the parent add_tool() method.

        Args:
            tool: The Tool instance to register

        Returns:
            The tool instance that was added

        Raises:
            OpenAIStrictModeValidationError: If the tool schema violates strict mode requirements
        """
        # Validate schema before adding the tool
        validate_openai_strict_mode_schema(tool.parameters, model_name=tool.name)

        # Delegate to parent to actually add the tool
        return super().add_tool(tool)

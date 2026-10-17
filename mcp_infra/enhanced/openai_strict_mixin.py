"""OpenAI strict mode validation mixin for FastMCP.

This mixin adds validation of tool input schemas against OpenAI's strict mode requirements
at tool registration time (immediately when add_tool() is called).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastmcp.server import FastMCP
from fastmcp.tools import Tool
from fastmcp.tools.function_tool import FunctionTool

from mcp_infra.tool_schemas import input_model_for_tool
from openai_utils.json_schema import openai_json_schema
from openai_utils.pydantic_strict_mode import validate_openai_strict_mode_schema

logger = logging.getLogger(__name__)


class OpenAIStrictModeMixin(FastMCP):
    """Mixin that validates tool schemas conform to OpenAI strict mode at registration time."""

    def add_tool(self, tool: Tool | Callable[..., Any]) -> Tool:
        """Normalize and validate tool schema during registration.

        Native FunctionTool inputs are rebuilt using the repository's
        OpenAI-compatible Pydantic schema generator before strict-mode validation.
        """
        # Delegate to parent first so Callable gets converted to Tool
        added_tool = super().add_tool(tool)

        # FastMCP builds the schema from the function signature. Rebuild it with
        # the repo's OpenAI-compatible Pydantic schema generator so nested models,
        # defaults, and discriminated unions follow the same strict rules.
        if isinstance(added_tool, FunctionTool):
            added_tool.parameters = openai_json_schema(input_model_for_tool(added_tool))

        # Validate schema after adding (tool is now guaranteed to be a Tool)
        validate_openai_strict_mode_schema(added_tool.parameters, model_name=added_tool.name)

        return added_tool

"""FlatTool - Tool that parses flat arguments into a Pydantic model.

This module is separate to avoid circular dependencies between mcp_infra and mcp_infra/enhanced.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.tools import Tool, ToolResult
from pydantic import BaseModel, ConfigDict, ValidationError

from agent_core.pydantic_utils import format_validation_error


class _EmptyModel(BaseModel):
    """Empty model for no-argument flat_model tools."""

    model_config = ConfigDict(extra="forbid")


class FlatTool[InputModelT: BaseModel, OutputT](Tool):
    """Tool that parses flat arguments into a Pydantic model.

    Extends Tool to add typed access to the input model.
    Use isinstance(tool, FlatTool) to check for flat tools and access input_model directly.
    """

    fn: Callable[..., Any]
    """The original function that takes a Pydantic model (or nothing for no-arg tools)."""

    input_model: type[InputModelT]
    """The Pydantic model for tool input parameters."""

    context_kwarg: str | None = None
    """Name of the context parameter, if the function accepts one."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Run the tool by parsing arguments into input_model and calling fn."""
        # Parse arguments directly into input model
        try:
            payload = self.input_model(**arguments)
        except ValidationError as e:
            raise ToolError(format_validation_error(e)) from e

        # Call original function
        if self.input_model is _EmptyModel:
            result = self.fn()
        elif self.context_kwarg:
            result = self.fn(payload, get_context())
        else:
            result = self.fn(payload)

        if inspect.isawaitable(result):
            result = await result

        return self.convert_result(result)

"""FlatTool - Tool that parses flat arguments into a Pydantic model.

This module is separate to avoid circular dependencies between mcp_infra and mcp_infra/enhanced.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

import pydantic_core
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.tools.tool import Tool, ToolResult, _convert_to_content
from fastmcp.utilities.types import Audio, File, Image
from mcp.types import ContentBlock
from pydantic import BaseModel, ConfigDict, ValidationError


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
            errors = json.loads(e.json())
            for err in errors:
                err.pop("url", None)
            raise ToolError(json.dumps(errors, indent=2)) from e

        # Call original function
        if self.input_model is _EmptyModel:
            result = self.fn()
        elif self.context_kwarg:
            result = self.fn(payload, get_context())
        else:
            result = self.fn(payload)

        if inspect.isawaitable(result):
            result = await result

        # Convert to ToolResult (adapted from FunctionTool.run)
        if isinstance(result, ToolResult):
            return result

        unstructured_result = _convert_to_content(result, serializer=self.serializer)

        if self.output_schema is None:
            # Handle MCP content types
            if isinstance(result, ContentBlock | Audio | Image | File) or (
                isinstance(result, list | tuple) and any(isinstance(item, ContentBlock) for item in result)
            ):
                return ToolResult(content=unstructured_result)
            # Try dict serialization
            try:
                structured_content = pydantic_core.to_jsonable_python(result)
                if isinstance(structured_content, dict):
                    return ToolResult(content=unstructured_result, structured_content=structured_content)
            except pydantic_core.PydanticSerializationError:
                pass
            return ToolResult(content=unstructured_result)

        wrap_result = self.output_schema.get("x-fastmcp-wrap-result")
        return ToolResult(content=unstructured_result, structured_content={"result": result} if wrap_result else result)

"""Register sync or async callables as tools without MCP.

    provider = DirectToolProvider()

    @provider.tool
    async def example(args: InputModel) -> ToolResult:
        '''Description exposed to the model.'''
        return ToolResult.text(f"Result: {args.value}")

Callables accept one annotated Pydantic model or no arguments (using EmptyArgs).
`@provider.tool(name="...")` overrides the Python name; the callable's docstring
supplies the tool description. Register the provider with Agent.create().

ToolResult passes through, strings become text, and None becomes text "OK".
BaseModel/list results become structured content via pydantic-core serialization.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, get_type_hints, overload

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import to_jsonable_python

from agent_core.tool_provider import ToolResult, ToolSchema
from openai_utils.json_schema import OpenAICompatibleSchema

# Tool functions can return ToolResult, str, BaseModel, list, None, or awaitables of any
ToolReturn = ToolResult | str | BaseModel | list | None
ToolFn = Callable[..., ToolReturn | Awaitable[ToolReturn]]


class EmptyArgs(BaseModel):
    """Empty args model for zero-parameter tools."""

    model_config = ConfigDict(extra="forbid")


@dataclass(slots=True)
class RegisteredTool:
    """A registered tool with its metadata and implementation."""

    name: str
    description: str
    parameters: type[BaseModel]
    fn: ToolFn


class DirectToolProvider:
    """Discover and invoke tools registered with `tool`."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    @overload
    def tool(self, fn: ToolFn) -> ToolFn: ...

    @overload
    def tool(self, *, name: str) -> Callable[[ToolFn], ToolFn]: ...

    def tool(self, fn: ToolFn | None = None, *, name: str | None = None) -> ToolFn | Callable[[ToolFn], ToolFn]:
        """Register a callable; its argument annotation supplies the input schema."""

        def register(func: ToolFn) -> ToolFn:
            tool_name = name if name is not None else func.__name__
            description = inspect.getdoc(func) or ""

            hints = get_type_hints(func)
            sig = inspect.signature(func)
            params = list(sig.parameters.values())

            if not params:
                # Zero-arg tool - use singleton empty args model
                param_type: type[BaseModel] = EmptyArgs
            else:
                first_param = params[0]
                param_type_hint = hints.get(first_param.name)
                if param_type_hint is None:
                    raise TypeError(f"Tool {tool_name} parameter '{first_param.name}' must have a type annotation")
                if not (isinstance(param_type_hint, type) and issubclass(param_type_hint, BaseModel)):
                    raise TypeError(
                        f"Tool {tool_name} parameter '{first_param.name}' must be a Pydantic BaseModel, "
                        f"got {param_type_hint}"
                    )
                param_type = param_type_hint

            self._tools[tool_name] = RegisteredTool(
                name=tool_name, description=description, parameters=param_type, fn=func
            )
            return func

        # Handle both @provider.tool and @provider.tool(name="...")
        if fn is not None:
            return register(fn)
        return register

    async def list_tools(self) -> list[ToolSchema]:
        """Return available tools."""
        return [
            ToolSchema(
                name=t.name,
                description=t.description,
                input_schema=t.parameters.model_json_schema(schema_generator=OpenAICompatibleSchema),
            )
            for t in self._tools.values()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute a tool and return the result."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.error(f"Unknown tool: {name}")

        try:
            validated_args = tool.parameters.model_validate(arguments)
        except ValidationError as e:
            return ToolResult.error(e.json(indent=2, include_url=False))

        try:
            result = tool.fn() if tool.parameters is EmptyArgs else tool.fn(validated_args)
            if isinstance(result, Awaitable):
                result = await result
        except Exception as e:
            return ToolResult.error(f"Tool error: {e}")

        if result is None:
            return ToolResult.text("OK")
        if isinstance(result, str):
            return ToolResult.text(result)
        if isinstance(result, ToolResult):
            return result
        # BaseModel, list, dict, or any other type - use pydantic-core serialization
        return ToolResult.structured(to_jsonable_python(result))

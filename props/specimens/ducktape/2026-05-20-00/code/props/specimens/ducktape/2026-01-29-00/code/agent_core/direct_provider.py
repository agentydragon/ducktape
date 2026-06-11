"""Direct tool provider for in-container agents without MCP overhead.

Wraps Python callables directly, avoiding MCP protocol overhead.

Usage:
    provider = DirectToolProvider()

    @provider.tool
    async def my_tool(args: MyArgsModel) -> ToolResult:
        '''Tool description from docstring.'''
        return ToolResult.text(f"Result: {args.value}")

    # Sync functions supported; can return str, Pydantic models, lists, or None
    @provider.tool
    def sync_tool(args: OtherArgsModel) -> str:
        '''Returns str (auto-converted to ToolResult.text).'''
        return "done"

    @provider.tool
    def structured_tool(args: InputModel) -> OutputModel:
        '''Returns Pydantic model (auto-converted to ToolResult.json).'''
        return OutputModel(result=args.value)

    @provider.tool
    def list_tool(args: QueryArgs) -> list[ItemModel]:
        '''Returns list of Pydantic models (auto-converted to ToolResult.json).'''
        return [ItemModel(id=1), ItemModel(id=2)]

    @provider.tool
    def exit_tool(args: ExitArgs) -> None:
        '''Returns None (auto-converted to ToolResult.text("OK")).'''
        do_exit_stuff()

    # Zero-arg tools are supported
    @provider.tool
    def list_items() -> str:
        '''No args needed.'''
        return "items: ..."

    agent = await Agent.create(tool_provider=provider, ...)
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, get_type_hints, overload

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from agent_core.tool_provider import ToolResult, ToolSchema
from openai_utils.json_schema import OpenAICompatibleSchema

# Tool functions can return ToolResult, str, BaseModel, list, None, or awaitables of any
ToolReturn = ToolResult | str | BaseModel | list | None
ToolFn = Callable[..., ToolReturn | Awaitable[ToolReturn]]


class _EmptyArgs(BaseModel):
    """Singleton empty args model for zero-arg tools."""


@dataclass(slots=True)
class RegisteredTool:
    """A registered tool with its metadata and implementation."""

    name: str
    description: str
    parameters: type[BaseModel]
    fn: ToolFn


class DirectToolProvider:
    """Tool provider that wraps Python callables directly.

    No MCP overhead - tools are registered via decorator. Tools can return
    ToolResult or str (auto-converted to ToolResult.text).
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    @overload
    def tool(self, fn: ToolFn) -> ToolFn: ...

    @overload
    def tool(self, *, name: str) -> Callable[[ToolFn], ToolFn]: ...

    def tool(self, fn: ToolFn | None = None, *, name: str | None = None) -> ToolFn | Callable[[ToolFn], ToolFn]:
        """Decorator to register a function as a tool.

        The function must:
        - Take a single Pydantic model argument
        - Return ToolResult or str (sync or async)
        - Have a docstring (used as tool description)

        Args:
            name: Override tool name (defaults to function name)
        """

        def register(func: ToolFn) -> ToolFn:
            tool_name = name if name is not None else func.__name__
            description = inspect.getdoc(func) or ""

            # Get the parameter type from type hints
            hints = get_type_hints(func)
            sig = inspect.signature(func)
            params = list(sig.parameters.values())

            if not params:
                # Zero-arg tool - use singleton empty args model
                param_type: type[BaseModel] = _EmptyArgs
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

        validated_args = tool.parameters.model_validate(arguments)

        # Call tool function, wrapping errors as ToolResult.error
        try:
            result = tool.fn() if tool.parameters is _EmptyArgs else tool.fn(validated_args)
            if isinstance(result, Awaitable):
                result = await result
        except Exception as e:
            return ToolResult.error(f"Tool error: {e}")

        # Convert result to ToolResult
        if result is None:
            return ToolResult.text("OK")
        if isinstance(result, str):
            return ToolResult.text(result)
        if isinstance(result, ToolResult):
            return result
        # BaseModel, list, dict, or any other type - use pydantic-core serialization
        return ToolResult.structured(to_jsonable_python(result))

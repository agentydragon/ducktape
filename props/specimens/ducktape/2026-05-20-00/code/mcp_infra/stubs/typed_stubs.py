import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast, get_origin, get_type_hints

from fastmcp.client import Client
from fastmcp.client.client import CallToolResult as FastMCPCallToolResult
from fastmcp.server import FastMCP
from pydantic import BaseModel, TypeAdapter

from mcp_infra.flat_tool import FlatTool


def _structured_content(result: FastMCPCallToolResult, *, tool_name: str) -> dict[str, Any]:
    sc = result.structured_content
    if sc is None:
        raise RuntimeError(f"{tool_name!r} did not return structured_content; tests require structured outputs")
    return TypeAdapter(dict[str, Any]).validate_python(sc)


class ToolStub[T_Out]:
    """Awaitable callable bound to a (session, tool_name, out_type)."""

    def __init__(self, session: Client, name: str, out_type: type[T_Out]) -> None:
        self._session = session
        self._name = name
        self._out_type = out_type

    async def __call__[T_In: BaseModel](self, payload: T_In) -> T_Out:
        args = payload.model_dump(exclude_none=False)
        result = await self._session.call_tool(name=self._name, arguments=args)

        if result.structured_content is not None:
            content = result.structured_content
            # FastMCP wraps non-object schemas (unions, primitives) in {"result": ...}
            # (x-fastmcp-wrap-result flag). Unwrap before validation.
            if isinstance(content, dict) and "result" in content and len(content) == 1:
                content = content["result"]
            return TypeAdapter(self._out_type).validate_python(content)

        # Fallback: no structured output
        raise RuntimeError(f"{self._name!r} did not return structured_content; tests require structured outputs")


def _resolve_output_type(hinted_output: object, out_model: object) -> type[Any]:
    if hinted_output is not None:
        if isinstance(hinted_output, type):
            return hinted_output
        origin = get_origin(hinted_output)
        if origin is not None or isinstance(hinted_output, types.UnionType):
            return cast(type[Any], hinted_output)
    if isinstance(out_model, type):
        return out_model
    return object


@dataclass(frozen=True)
class ToolModels:
    # Public types tests should use
    Input: type[BaseModel] | None
    Output: type[Any]  # This should be a type, not an instance
    # Internal wiring details for FastMCP registry
    _arg_model: type[BaseModel] | None = None
    # No output wrapping; servers should return structured content matching Output


class TypedClient:
    """Factory for typed tool call stubs bound to a session.

    Usage:
      # Manual typing
      client = TypedClient(session)
      sandbox_exec = client.stub("sandbox_exec", SandboxExecResult)
      res = await sandbox_exec(ExecArgs(...))

      # In-proc typed client (introspects FastMCP server registry)
      client = TypedClient.from_server(server, session)
      ExecArgs = client.models["sandbox_exec"].Input
      res = await client.sandbox_exec(ExecArgs(...))
    """

    def __init__(self, session: Client) -> None:
        self._session = session
        self._models: dict[str, ToolModels] = {}

    def stub[T_Out](self, name: str, out_type: type[T_Out]) -> ToolStub[T_Out]:
        return ToolStub(self._session, name, out_type)

    @property
    def models(self) -> dict[str, ToolModels]:
        return self._models

    @classmethod
    def from_server(cls, server: FastMCP, session: Client) -> "TypedClient":
        """Create a TypedClient introspecting FastMCP's tool registry.

        Requires a server created via FastMCP. Introspects FlatTool instances
        for input_model and return type annotations.
        """
        components = server._local_provider._components

        client = cls(session)
        for component in components.values():
            # Only FlatTool has the typed metadata we need
            if not isinstance(component, FlatTool):
                continue

            input_type: type[BaseModel] = component.input_model

            # Get output type from function's return annotation
            try:
                hints = get_type_hints(component.fn, include_extras=True)
                hinted_output = hints.get("return")
            except (NameError, TypeError, AttributeError):
                hinted_output = None

            output_type = _resolve_output_type(hinted_output, hinted_output)
            client._models[component.name] = ToolModels(Input=input_type, Output=output_type, _arg_model=input_type)

        return client

    def error(self, name: str) -> Callable[[BaseModel], Awaitable[str]]:
        """Return an async callable that invokes the tool expecting failure.

        FastMCP's default raise_on_error=True means tool errors surface as
        exceptions. The returned string is str(exc) from that exception.
        """
        models = self._models.get(name)
        if not models:
            raise AttributeError(name)
        session = self._session

        async def _err(payload: BaseModel) -> str:
            if models.Input is not None and not isinstance(payload, models.Input):
                raise TypeError(f"{name} expects {models.Input.__name__}, got {type(payload).__name__}")
            args_dict = payload.model_dump(exclude_none=False)
            try:
                result = await session.call_tool(name=name, arguments=args_dict)
            except Exception as exc:
                return str(exc)
            if not result.is_error:
                raise AssertionError("expected tool error")
            raise AssertionError(f"expected exception from {name}, got is_error=True result instead")

        return _err

    def __getattr__(self, name: str) -> Callable[[BaseModel], Awaitable[object]]:
        # Provide convenient client.tool_name(ExecArgs(...)) form when we have models
        models = self._models.get(name)
        if not models:
            raise AttributeError(name)
        tool_stub: ToolStub[Any] = self.stub(name, models.Output)

        async def _call(payload: BaseModel) -> object:
            return await tool_stub(payload)

        return _call

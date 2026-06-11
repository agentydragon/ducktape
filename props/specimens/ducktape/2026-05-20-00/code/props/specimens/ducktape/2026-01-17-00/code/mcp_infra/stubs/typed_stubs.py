from __future__ import annotations

import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast, get_origin

from fastmcp.client import Client
from fastmcp.client.client import CallToolResult as FastMCPCallToolResult
from fastmcp.server import FastMCP
from mcp import types as mcp_types
from pydantic import BaseModel, TypeAdapter

from mcp_infra.client_helpers import extract_error_detail_from_fastmcp

T_In = TypeVar("T_In", bound=BaseModel)
T_Out = TypeVar("T_Out")


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

    async def __call__(self, payload: T_In) -> T_Out:
        args = payload.model_dump(exclude_none=False)
        result = await self._session.call_tool(name=self._name, arguments=args)

        # FastMCP wraps non-object schemas (unions, primitives) in {"result": ...}
        # (see fastmcp/src/fastmcp/tools/tool.py). For wrapped results, FastMCP's client provides
        # a .data field with unwrapped content (see fastmcp/src/fastmcp/client/client.py).
        #
        # Strategy:
        # - For wrapped results: use .data (dict with unwrapped content)
        # - For object results: use .structured_content (dict), NOT .data (Pydantic model instance)
        #
        # We distinguish by checking if .data is a dict (usable) vs Pydantic model (needs conversion).
        if result.data is not None and isinstance(result.data, dict):
            # FastMCP unwrapped a union/primitive for us
            return TypeAdapter(self._out_type).validate_python(result.data)

        if result.structured_content is not None:
            # Regular object schema - use the dict directly
            return TypeAdapter(self._out_type).validate_python(result.structured_content)

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


def _extract_error_message(resp: FastMCPCallToolResult) -> str:
    detail = extract_error_detail_from_fastmcp(resp)
    if detail:
        return cast(str, detail)
    nontext: list[str] = [
        type(block).__name__ for block in resp.content or [] if not isinstance(block, mcp_types.TextContent)
    ]
    if nontext:
        raise NotImplementedError(f"Unsupported tool error content types: {', '.join(nontext)}")
    return "tool error"


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

    def stub(self, name: str, out_type: type[T_Out]) -> ToolStub[T_Out]:
        return ToolStub(self._session, name, out_type)

    @property
    def models(self) -> dict[str, ToolModels]:
        return self._models

    @classmethod
    def from_server(cls, server: FastMCP, session: Client) -> TypedClient:
        """Create a TypedClient introspecting FastMCP's tool registry.

        Requires a server created via FastMCP. Uses server._tool_manager.list_tools()
        and reads each tool.fn_metadata.arg_model/output_model.
        """
        # Access the internal tool manager and fetch local tools synchronously
        try:
            tm = server._tool_manager
        except AttributeError as exc:
            raise RuntimeError("Server does not expose _tool_manager") from exc
        # Prefer local tools; mounted tools aren't needed for typed tests here
        try:
            tools_by_name = tm._tools
        except AttributeError as exc:
            raise RuntimeError("Server tool manager does not expose _tools") from exc
        tools = list(tools_by_name.values())

        client = cls(session)
        for t in tools:
            try:
                fm = t.fn_metadata  # type: ignore[attr-defined]
            except AttributeError:
                fm = None
            try:
                fn = t.fn  # type: ignore[attr-defined]
            except AttributeError:
                fn = None
            hinted_input = None
            hinted_output = None
            if fn is not None:
                try:
                    hinted_input = fn._mcp_flat_input_model
                except AttributeError:
                    hinted_input = None
                try:
                    hinted_output = fn._mcp_flat_output_model
                except AttributeError:
                    hinted_output = None
            if fm is None:
                # Fall back to flat-model hints only
                arg_model = hinted_input
                out_model = hinted_output
                if not (isinstance(arg_model, type) and issubclass(arg_model, BaseModel)):
                    continue
            else:
                arg_model = fm.arg_model
                out_model = fm.output_model
                if out_model is None or arg_model is None:
                    continue

            if isinstance(hinted_input, type) and issubclass(hinted_input, BaseModel):
                input_type: type[BaseModel] | None = hinted_input
            elif isinstance(arg_model, type) and issubclass(arg_model, BaseModel):
                input_type = arg_model
            else:
                input_type = None

            try:
                tool_key = t.key
            except AttributeError:
                try:
                    tool_key = t.name
                except AttributeError:
                    tool_key = None
            if not isinstance(tool_key, str) or not tool_key:
                continue
            output_type = _resolve_output_type(hinted_output, out_model)
            client._models[tool_key] = ToolModels(Input=input_type, Output=output_type, _arg_model=arg_model)
        return client

    def error(self, name: str) -> Callable[[BaseModel], Awaitable[str]]:
        models = self._models.get(name)
        if not models:
            raise AttributeError(name)
        session = self._session

        async def _err(payload: BaseModel) -> str:
            if models.Input is not None and not isinstance(payload, models.Input):
                raise TypeError(f"{name} expects {models.Input.__name__}, got {type(payload).__name__}")
            args_dict = payload.model_dump(exclude_none=False)
            # Call; FastMCP raises on tool error by default. Capture and return message.
            try:
                result = await session.call_tool(name=name, arguments=args_dict)
            except Exception as exc:
                return str(exc)
            if not result.is_error:
                raise AssertionError("expected tool error")
            return _extract_error_message(result)

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

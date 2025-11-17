from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import types
from typing import Any, Generic, TypeVar, cast, get_origin

from fastmcp.client import Client
from fastmcp.client.client import CallToolResult as FMCallToolResult
from fastmcp.server import FastMCP
from mcp import types as mcp_types
from mcp.types import CallToolResult
from pydantic import BaseModel, TypeAdapter, ValidationError

from adgn.mcp._shared.calltool import to_pydantic
from adgn.mcp._shared.client_helpers import extract_error_detail

StructuredContent = BaseModel | dict[str, Any] | list[Any] | str | int | float | bool | None

# We use the concrete FastMCP Client type for sessions in tests


T_Out = TypeVar("T_Out")

_CALL_RESULT_TYPE_ERR = "expected fastmcp.client CallToolResult, got {typename}"


def _normalize_result(raw: object, *, tool_name: str) -> CallToolResult:
    """Normalize FastMCP client results to the canonical Pydantic CallToolResult."""
    if not isinstance(raw, FMCallToolResult):
        raise TypeError(f"{tool_name!r}: {_CALL_RESULT_TYPE_ERR.format(typename=type(raw).__name__)}")
    return to_pydantic(raw)


def _structured_content(result: CallToolResult, *, tool_name: str) -> StructuredContent:
    sc = cast(StructuredContent | None, result.structuredContent)
    if sc is None:
        raise RuntimeError(f"{tool_name!r} did not return structuredContent; tests require structured outputs")
    return sc


async def _call_normalized(session: Client, tool_name: str, arguments: dict[str, object] | None) -> CallToolResult:
    """Call a FastMCP tool and normalize the result to the Pydantic CallToolResult."""
    raw = await session.call_tool(name=tool_name, arguments=arguments)
    return _normalize_result(raw, tool_name=tool_name)


async def _call_structured(
    session: Client, tool_name: str, arguments: dict[str, object] | None
) -> tuple[CallToolResult, StructuredContent]:
    """Call a FastMCP tool and return both the normalized result and structured content."""
    result = await _call_normalized(session, tool_name, arguments)
    return result, _structured_content(result, tool_name=tool_name)


def _build_arguments(
    payload: BaseModel | dict[str, object],
    *,
    input_model: type[BaseModel] | None,
    wrapper_field: str | None,
    exclude_none: bool,
    tool_name: str,
) -> dict[str, object] | None:
    if input_model is not None and not isinstance(payload, input_model):
        raise TypeError(f"{tool_name} expects {input_model.__name__}, got {type(payload).__name__}")
    # model_dump() returns dict[str, Any] which is compatible with dict[str, object]
    data: dict[str, object] = (
        payload.model_dump(exclude_none=exclude_none) if isinstance(payload, BaseModel) else payload
    )
    if wrapper_field:
        return {wrapper_field: data}
    return data


async def call_tool_typed(
    session: Client,
    name: str,
    payload: BaseModel | dict[str, object],
    out_type: type[T_Out],
    *,
    exclude_none: bool = True,
    input_model: type[BaseModel] | None = None,
    wrapper_field: str | None = None,
) -> T_Out:
    """Call an MCP tool with a Pydantic input and parse a Pydantic output.

    Requires structuredContent from the server; raises otherwise.
    """
    args = _build_arguments(
        payload, input_model=input_model, wrapper_field=wrapper_field, exclude_none=exclude_none, tool_name=name
    )
    _result, structured = await _call_structured(session, name, args)
    adapter: TypeAdapter[T_Out] = TypeAdapter(out_type)
    try:
        parsed = adapter.validate_python(structured)
    except ValidationError:
        if isinstance(structured, dict) and "result" in structured:
            parsed = adapter.validate_python(structured["result"])
        else:
            raise
    return parsed


class ToolStub(Generic[T_Out]):
    """Awaitable callable bound to a (session, tool_name, out_type)."""

    def __init__(
        self,
        session: Client,
        name: str,
        out_type: type[T_Out],
        *,
        exclude_none: bool = True,
        input_model: type[BaseModel] | None = None,
        wrapper_field: str | None = None,
    ) -> None:
        self._session = session
        self._name = name
        self._out_type = out_type
        self._exclude_none = exclude_none
        self._input_model = input_model
        self._wrapper_field = wrapper_field

    async def __call__(self, payload: BaseModel | dict[str, object]) -> T_Out:
        return await call_tool_typed(
            self._session,
            self._name,
            payload,
            self._out_type,
            exclude_none=self._exclude_none,
            input_model=self._input_model,
            wrapper_field=self._wrapper_field,
        )


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
    _wrapper_field: str | None = None
    # No output wrapping; servers should return structured content matching Output


def _extract_error_message(resp: CallToolResult) -> str:
    detail = extract_error_detail(resp)
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

    def __init__(self, session: Client, *, exclude_none: bool = True) -> None:
        self._session = session
        self._exclude_none = exclude_none
        self._models: dict[str, ToolModels] = {}

    def stub(self, name: str, out_type: type[T_Out]) -> ToolStub[T_Out]:
        meta = self._models.get(name)
        input_model = meta.Input if meta else None
        wrapper_field = meta._wrapper_field if meta else None
        return ToolStub(
            self._session,
            name,
            out_type,
            exclude_none=self._exclude_none,
            input_model=input_model,
            wrapper_field=wrapper_field,
        )

    @property
    def models(self) -> dict[str, ToolModels]:
        return self._models

    @classmethod
    def from_server(cls, server: FastMCP, session: Client, *, exclude_none: bool = True) -> TypedClient:
        """Create a TypedClient introspecting FastMCP's tool registry.

        Requires a server created via FastMCP. Uses server._tool_manager.list_tools()
        and reads each tool.fn_metadata.arg_model/output_model.

        Note: This method intentionally accesses private attributes of FastMCP
        objects for test introspection. These are not public APIs but are needed
        to extract type information for creating typed test stubs.
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

        client = cls(session, exclude_none=exclude_none)
        for t in tools:
            try:
                fm = t.fn_metadata
            except AttributeError:
                fm = None
            try:
                fn = t.fn
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

            wrapper_field = None
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
            client._models[tool_key] = ToolModels(
                Input=input_type, Output=output_type, _arg_model=arg_model, _wrapper_field=wrapper_field
            )
        return client

    def error(self, name: str) -> Callable[[BaseModel], Awaitable[str]]:
        models = self._models.get(name)
        if not models:
            raise AttributeError(name)
        exclude_none = self._exclude_none
        session = self._session

        async def _err(payload: BaseModel) -> str:
            args_dict = _build_arguments(
                payload,
                input_model=models.Input,
                wrapper_field=models._wrapper_field,
                exclude_none=exclude_none,
                tool_name=name,
            )
            # Call; FastMCP raises on tool error by default. Capture and return message.
            try:
                result = await _call_normalized(session, name, args_dict)
            except Exception as exc:
                return str(exc)
            if not result.isError:
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

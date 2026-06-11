from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable
from types import UnionType
from typing import Annotated, Any, TypeVar, Union, get_args, get_origin, get_type_hints

from fastmcp.server import FastMCP
from mcp import types as mcp_types
from mcp.types import ToolAnnotations
from pydantic import BaseModel, TypeAdapter, ValidationError

from mcp_infra.flat_tool import FlatTool, _EmptyModel
from openai_utils.json_schema import openai_json_schema

# Re-export for backwards compatibility
__all__ = ["FlatModelMixin", "FlatTool"]

logger = logging.getLogger(__name__)

InputModelT = TypeVar("InputModelT", bound=BaseModel)
OutputT = TypeVar("OutputT")


def _collect_type_hints(fn: Callable[..., Any]) -> dict[str, Any]:
    globs = fn.__globals__
    try:
        return get_type_hints(fn, globalns=globs, localns=globs, include_extras=True)
    except (NameError, TypeError, AttributeError):
        return {}


def _resolve_base_model(
    *, name: str, annotation: Any, hints: dict[str, Any], globals_ns: dict[str, Any], error_prefix: str
) -> type[BaseModel]:
    cand = hints.get(name, annotation)
    if isinstance(cand, str):
        try:
            cand = globals_ns[cand]
        except Exception as exc:
            raise NotImplementedError(
                f"mcp_flat_model requires real types for {error_prefix}; string annotations not resolved. "
                "Move models to module scope to allow resolution."
            ) from exc
    if not (isinstance(cand, type) and issubclass(cand, BaseModel)):
        raise TypeError(f"{error_prefix} must be a Pydantic BaseModel subclass")
    return cand


def _ensure_model_rebuild(model: type[BaseModel], *, kind: str) -> None:
    try:
        model.model_rebuild()
    except AttributeError as exc:
        raise TypeError(f"{kind} model must be a Pydantic BaseModel with model_rebuild()") from exc
    except Exception as exc:
        logger.debug("model_rebuild() on %s failed: %s", kind, exc)


def _extract_signature_params(fn: Callable[..., Any]) -> tuple[inspect.Parameter | None, str | None]:
    """Extract payload parameter and context parameter name from function signature."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if len(params) not in (0, 1, 2):
        raise TypeError("@flat_model expects 0 parameters (no-arg), payload model parameter, or payload + Context")
    if len(params) == 0:
        return None, None
    payload_param = params[0]
    context_name: str | None = None
    if len(params) == 2:
        context_param = params[1]
        if context_param.kind not in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            raise TypeError("@flat_model context parameter must be positional-or-keyword or keyword-only")
        context_name = context_param.name
    return payload_param, context_name


class FlatModelMixin(FastMCP):
    """Mixin that provides flat-model tool support with structured ValidationError formatting."""

    async def _call_tool_mcp(
        self, key: str, arguments: dict[str, Any]
    ) -> list[mcp_types.ContentBlock] | tuple[list[mcp_types.ContentBlock], dict[str, Any]] | mcp_types.CallToolResult:
        """Override to format ValidationErrors from flat model tools as flat JSON."""
        try:
            result: (
                list[mcp_types.ContentBlock]
                | tuple[list[mcp_types.ContentBlock], dict[str, Any]]
                | mcp_types.CallToolResult
            ) = await super()._call_tool_mcp(key, arguments)
            return result
        except ValidationError as e:
            errors = json.loads(e.json())
            for err in errors:
                err.pop("url", None)
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text=json.dumps(errors, indent=2))], isError=True
            )

    def flat_model(
        self,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        tags: set[str] | None = None,
        structured_output: bool = False,
    ) -> Callable[
        [Callable[[], OutputT] | Callable[[InputModelT], OutputT] | Callable[[InputModelT, Any], OutputT]],
        FlatTool[InputModelT, OutputT],
    ]:
        """Decorator for flat-model tools that flattens Pydantic model parameters.

        structured_output: If True, enables structured output with schema from return type annotation.
        If False (default), tools return unstructured content only.
        """

        def outer(
            fn: Callable[[], OutputT] | Callable[[InputModelT], OutputT] | Callable[[InputModelT, Any], OutputT],
        ) -> FlatTool[InputModelT, OutputT]:
            if not inspect.isfunction(fn):
                raise TypeError("@flat_model requires a plain function (not a callable object)")

            # Extract signature and resolve input model
            payload_param, context_name = _extract_signature_params(fn)
            hints = _collect_type_hints(fn)
            globs = fn.__globals__

            # Handle no-arg functions
            model_in: type[BaseModel]
            if payload_param is None:
                model_in = _EmptyModel
            else:
                model_in = _resolve_base_model(
                    name=payload_param.name,
                    annotation=payload_param.annotation,
                    hints=hints,
                    globals_ns=globs,
                    error_prefix="Parameter",
                )

            # Resolve output type from function signature
            sig = inspect.signature(fn)
            output_type = hints.get("return", sig.return_annotation)

            if structured_output and output_type is inspect.Signature.empty:
                raise TypeError(
                    "Return annotation is required for flat-model tools with structured_output=True. "
                    f"Function {fn.__name__!r} has no return type annotation."
                )

            if isinstance(output_type, str):
                try:
                    output_type = globs[output_type]
                except Exception as exc:
                    raise NotImplementedError(
                        "flat_model requires real types for output; string annotations not resolved. "
                        "Move models to module scope."
                    ) from exc

            # Rebuild input model to resolve forward references
            _ensure_model_rebuild(model_in, kind="Input")

            # Only rebuild output model if structured output is enabled
            if structured_output and output_type is not inspect.Signature.empty:
                rt = output_type
                if get_origin(rt) is Annotated:
                    rt = get_args(rt)[0]
                if isinstance(rt, type) and issubclass(rt, BaseModel):
                    _ensure_model_rebuild(rt, kind="Output")

            # Generate input schema from Pydantic model
            input_schema = openai_json_schema(model_in)

            # Generate output schema only if structured output is enabled
            output_schema: dict[str, Any] | None = None
            if structured_output:
                is_union = get_origin(output_type) in (Union, UnionType)
                if get_origin(output_type) is Annotated:
                    base_type = get_args(output_type)[0]
                    is_union = get_origin(base_type) in (Union, UnionType)

                if isinstance(output_type, type) and issubclass(output_type, BaseModel):
                    output_schema = openai_json_schema(output_type)
                else:
                    output_schema = TypeAdapter(output_type).json_schema()

                # FastMCP wraps union types in {"result": ...} for MCP protocol compatibility
                if is_union:
                    defs = output_schema.pop("$defs", None)
                    wrapped_schema: dict[str, Any] = {
                        "type": "object",
                        "properties": {"result": output_schema},
                        "required": ["result"],
                        "x-fastmcp-wrap-result": True,
                    }
                    if defs:
                        wrapped_schema["$defs"] = defs
                    output_schema = wrapped_schema

            # Create FlatTool directly with original function
            tool: FlatTool[InputModelT, OutputT] = FlatTool(
                fn=fn,
                input_model=model_in,
                name=name or fn.__name__,
                title=title,
                description=description or inspect.getdoc(fn),
                parameters=input_schema,
                output_schema=output_schema,
                annotations=annotations,
                tags=tags or set(),
                enabled=True,
                context_kwarg=context_name,
            )

            self.add_tool(tool)
            return tool

        return outer

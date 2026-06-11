from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Callable
from types import UnionType
from typing import Annotated, Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from fastmcp.exceptions import ToolError
from fastmcp.server import FastMCP
from fastmcp.server.context import Context
from fastmcp.tools.tool import FunctionTool
from mcp import types as mcp_types
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic_core import PydanticUndefined

from openai_utils.json_schema import openai_json_schema

logger = logging.getLogger(__name__)

InputModelT = TypeVar("InputModelT", bound=BaseModel)
OutputModelT = TypeVar("OutputModelT")
RegisterTool = Callable[[Callable[..., Any], dict[str, Any]], Callable[..., Any]]


class _EmptyModel(BaseModel):
    """Empty model for no-argument flat_model tools."""

    model_config = ConfigDict(extra="forbid")


class FlatWrapper:
    """Wrapper that holds MCP metadata and flattens Pydantic model parameters."""

    def __init__(
        self,
        fn: Callable[..., Any],
        model_in: type[BaseModel],
        model_out: Any,
        context_param: inspect.Parameter | None = None,
        is_async: bool = False,
    ):
        self._fn = fn
        self._mcp_flat_input_model = model_in
        self._mcp_flat_output_model = model_out if model_out is not inspect.Signature.empty else None
        self._context_param = context_param
        self._context_name = context_param.name if context_param else None
        self._is_async = is_async

        # Build signature for the flattened function
        self.__signature__ = _make_flat_signature_from_model(
            model_in, return_type=model_out, context_param=context_param
        )

        # Copy function metadata
        functools.update_wrapper(self, fn)

    def _prepare_call(self, kwargs: dict[str, Any]) -> tuple[BaseModel, Any | None]:
        """Prepare arguments for function call."""
        # Extract payload kwargs (exclude context parameter)
        payload_kwargs = {k: v for k, v in kwargs.items() if k != self._context_name}

        # Create the model instance, catching validation errors
        try:
            payload = self._mcp_flat_input_model(**payload_kwargs)
        except ValidationError as e:
            # Get JSON-safe errors (Pydantic's .json() handles exception serialization)
            errors = json.loads(e.json())
            # Strip documentation URLs
            for err in errors:
                err.pop("url", None)
            # Raise ToolError with structured JSON for agent consumption
            raise ToolError(json.dumps(errors, indent=2)) from e

        # Get context value if needed
        ctx_value = kwargs.get(self._context_name) if self._context_name else None

        return payload, ctx_value

    def __call__(self, **kwargs: Any) -> Any:
        """Execute the wrapped function with flattened parameters (sync version)."""
        if self._is_async:
            raise RuntimeError("Cannot call async function synchronously. Use await or async context.")

        payload, ctx_value = self._prepare_call(kwargs)

        # Handle no-arg functions (using _EmptyModel)
        if self._mcp_flat_input_model is _EmptyModel:
            return self._fn()

        # Call with or without context
        if self._context_param is None or ctx_value is None:
            return self._fn(payload)
        return self._fn(payload, ctx_value)

    async def __acall__(self, **kwargs: Any) -> Any:
        """Execute the wrapped async function with flattened parameters."""
        if not self._is_async:
            # For sync functions, just call them normally
            return self.__call__(**kwargs)

        payload, ctx_value = self._prepare_call(kwargs)

        # Handle no-arg functions (using _EmptyModel)
        if self._mcp_flat_input_model is _EmptyModel:
            return await self._fn()

        # Call with or without context
        if self._context_param is None or ctx_value is None:
            return await self._fn(payload)
        return await self._fn(payload, ctx_value)


def _make_flat_signature_from_model(
    model: type[BaseModel], *, return_type: Any, context_param: inspect.Parameter | None = None
) -> inspect.Signature:
    params: list[inspect.Parameter] = []
    for name, fld in model.model_fields.items():
        ann = fld.annotation
        field_kwargs: dict[str, Any] = {"description": fld.description}
        if fld.alias:
            field_kwargs["alias"] = fld.alias
        if fld.default is not PydanticUndefined:
            field_kwargs["default"] = fld.default
        default_factory = fld.default_factory
        if default_factory is not None and default_factory is not PydanticUndefined:
            field_kwargs["default_factory"] = default_factory
        annotated_type: Any = Any if ann in (inspect._empty, None) else ann
        annotated = Annotated[annotated_type, Field(**field_kwargs)]
        if default_factory is not None or fld.default is not PydanticUndefined:
            param_default = fld.default if fld.default is not PydanticUndefined else inspect._empty
        else:
            param_default = inspect._empty
        params.append(
            inspect.Parameter(
                name=name, kind=inspect.Parameter.KEYWORD_ONLY, default=param_default, annotation=annotated
            )
        )
    if context_param is not None:
        ctx_default = context_param.default
        ctx_annotation = context_param.annotation
        if ctx_annotation in (inspect._empty, None):
            ctx_annotation = Any
        params.append(
            inspect.Parameter(
                name=context_param.name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=ctx_default,
                annotation=ctx_annotation,
            )
        )
    return inspect.Signature(parameters=params, return_annotation=return_type)


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
        except Exception as exc:  # pragma: no cover - mirrors previous behaviour
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
    except Exception as exc:  # pragma: no cover - debug logging path
        logger.debug("model_rebuild() on %s failed: %s", kind, exc)


def _extract_signature_params(
    fn: Callable[..., Any],
) -> tuple[inspect.Parameter | None, inspect.Parameter | None, str | None]:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if len(params) not in (0, 1, 2):
        raise TypeError("@mcp_flat_model expects 0 parameters (no-arg), payload model parameter, or payload + Context")
    if len(params) == 0:
        # No-arg function
        return None, None, None
    payload_param = params[0]
    context_param: inspect.Parameter | None = None
    if len(params) == 2:
        context_param = params[1]
        if context_param.kind not in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            raise TypeError("@mcp_flat_model context parameter must be positional-or-keyword or keyword-only")
    context_name = context_param.name if context_param is not None else None
    return payload_param, context_param, context_name


def _transfer_mcp_metadata(target: Callable[..., Any], wrapper: FlatWrapper) -> None:
    """Transfer MCP metadata from FlatWrapper to a wrapper function."""
    target._mcp_flat_input_model = wrapper._mcp_flat_input_model  # type: ignore[attr-defined]
    target._mcp_flat_output_model = wrapper._mcp_flat_output_model  # type: ignore[attr-defined]
    target.__signature__ = wrapper.__signature__  # type: ignore[attr-defined]


def _build_flat_wrapper(
    fn: Callable[..., Any],
    *,
    model_in: type[BaseModel],
    model_out: Any,
    context_param: inspect.Parameter | None,
    context_name: str | None,
) -> Callable[..., Any]:
    """Build a FlatWrapper instance that flattens the model parameters."""
    is_async = inspect.iscoroutinefunction(fn)
    wrapper = FlatWrapper(fn=fn, model_in=model_in, model_out=model_out, context_param=context_param, is_async=is_async)

    # For async functions, create a proper async wrapper
    if is_async:

        @functools.wraps(fn)
        async def async_wrapper(**kwargs: Any) -> Any:
            return await wrapper.__acall__(**kwargs)

        _transfer_mcp_metadata(async_wrapper, wrapper)
        return async_wrapper

    # For sync functions, also create a proper function wrapper (not just the FlatWrapper instance)
    # This is required for fastmcp 2.13+ which validates that @tool receives a real function
    @functools.wraps(fn)
    def sync_wrapper(**kwargs: Any) -> Any:
        return wrapper.__call__(**kwargs)

    _transfer_mcp_metadata(sync_wrapper, wrapper)
    return sync_wrapper


def _apply_wrapper_metadata(
    wrapper: Callable[..., Any], *, model_in: type[BaseModel], model_out: Any, context_param: inspect.Parameter | None
) -> None:
    signature = _make_flat_signature_from_model(model_in, return_type=model_out, context_param=context_param)
    # Replace wrapper's signature with flattened model parameters for FastMCP
    wrapper.__signature__ = signature  # type: ignore[attr-defined]

    def _build_param_annotations(model: type[BaseModel], *, return_type: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for pname, fld in model.model_fields.items():
            pann = fld.annotation
            field_kwargs: dict[str, Any] = {"description": fld.description}
            if fld.alias:
                field_kwargs["alias"] = fld.alias
            annotated_type: Any = Any if pann in (inspect._empty, None) else pann
            out[pname] = Annotated[annotated_type, Field(**field_kwargs)]
        if context_param is not None:
            ctx_annotation = context_param.annotation
            if ctx_annotation in (inspect._empty, None):
                ctx_annotation = Any
            out[context_param.name] = ctx_annotation
        out["return"] = return_type
        return out

    wrapper.__annotations__ = _build_param_annotations(model_in, return_type=model_out)


class FlatModelMixin(FastMCP):
    """Mixin that provides flat-model tool support with structured ValidationError formatting."""

    async def _call_tool_mcp(
        self, key: str, arguments: dict[str, Any]
    ) -> list[mcp_types.ContentBlock] | tuple[list[mcp_types.ContentBlock], dict[str, Any]] | mcp_types.CallToolResult:
        """Override to format ValidationErrors from flat model tools as flat JSON.

        When FastMCP validates tool arguments before calling our flat wrapper, ValidationErrors
        are raised before our wrapper's error formatting runs. This override catches those errors
        and formats them consistently with the flat error format (JSON array of error objects).
        """
        try:
            result: (
                list[mcp_types.ContentBlock]
                | tuple[list[mcp_types.ContentBlock], dict[str, Any]]
                | mcp_types.CallToolResult
            ) = await super()._call_tool_mcp(key, arguments)
            return result
        except ValidationError as e:
            # Format validation errors as flat JSON (same as FlatWrapper._prepare_call)
            errors = json.loads(e.json())
            for err in errors:
                err.pop("url", None)
            # Return as MCP error result
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
        [
            Callable[[], OutputModelT]
            | Callable[[InputModelT], OutputModelT]
            | Callable[[InputModelT, Context], OutputModelT]
        ],
        FunctionTool,
    ]:
        """Decorator for flat-model tools that flattens Pydantic model parameters.

        structured_output: If True, enables structured output with schema from return type annotation.
        If False (default), tools return unstructured content only.

        ═══════════════════════════════════════════════════════════════════════════════
        CRITICAL: NO SCHEMA POST-PROCESSING OR MONKEYPATCHING ALLOWED HERE OR ANYWHERE.
        ═══════════════════════════════════════════════════════════════════════════════

        Pydantic models MUST generate correct JSON schemas directly via model_config.
        FastMCP MUST accept and transmit those schemas as-is without modification.

        If schemas don't match OpenAI strict mode requirements (additionalProperties, etc.),
        fix the ROOT CAUSE:
          - Pydantic model definitions (model_config, Field annotations, etc.)
          - FastMCP's schema generation logic

        DO NOT fix by massaging/patching/monkeypatching schemas after generation.
        ═══════════════════════════════════════════════════════════════════════════════
        """

        def outer(
            fn: Callable[[], OutputModelT]
            | Callable[[InputModelT], OutputModelT]
            | Callable[[InputModelT, Context], OutputModelT],
        ) -> FunctionTool:
            if not inspect.isfunction(fn):
                raise TypeError("@flat_model requires a plain function (not a callable object)")

            # Extract signature and resolve input model
            payload_param, context_param, context_name = _extract_signature_params(fn)
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

            # Resolve output model from function signature
            sig = inspect.signature(fn)
            model_out = hints.get("return", sig.return_annotation)

            # Return annotation is only required if structured_output is enabled
            if structured_output and model_out is inspect.Signature.empty:
                raise TypeError(
                    "Return annotation is required for flat-model tools with structured_output=True. "
                    f"Function {fn.__name__!r} has no return type annotation."
                )

            if isinstance(model_out, str):
                try:
                    model_out = globs[model_out]
                except Exception as exc:
                    raise NotImplementedError(
                        "flat_model requires real types for output; string annotations not resolved. "
                        "Move models to module scope."
                    ) from exc

            # Rebuild models to resolve forward references
            _ensure_model_rebuild(model_in, kind="Input")

            # Only rebuild output model if structured output is enabled and we have a valid return annotation
            if structured_output and model_out is not inspect.Signature.empty:
                rt = model_out
                if get_origin(rt) is Annotated:
                    rt = get_args(rt)[0]
                if isinstance(rt, type) and issubclass(rt, BaseModel):
                    _ensure_model_rebuild(rt, kind="Output")

            # Build the flat wrapper function
            wrapper = _build_flat_wrapper(
                fn, model_in=model_in, model_out=model_out, context_param=context_param, context_name=context_name
            )

            # Apply flattened signature to wrapper (for introspection)
            _apply_wrapper_metadata(wrapper, model_in=model_in, model_out=model_out, context_param=context_param)

            # Generate input schema DIRECTLY from the Pydantic model
            # This preserves model_config (extra="forbid" -> additionalProperties: false)
            # Use OpenAICompatibleSchema to convert oneOf (discriminated unions) to anyOf
            input_schema = openai_json_schema(model_in)

            # Generate output schema only if structured output is enabled
            output_schema: dict[str, Any] | None = None
            if structured_output:
                # Check if output is a union type (for wrapping)
                is_union = get_origin(model_out) in (Union, UnionType)
                if get_origin(model_out) is Annotated:
                    base_type = get_args(model_out)[0]
                    is_union = get_origin(base_type) in (Union, UnionType)

                # Generate schema: use openai_json_schema for BaseModel, TypeAdapter otherwise
                if isinstance(model_out, type) and issubclass(model_out, BaseModel):
                    output_schema = openai_json_schema(model_out)
                else:
                    # Use TypeAdapter for unions, Annotated types, and other non-BaseModel types
                    # TypeAdapter doesn't support schema_generator parameter, so we use json_schema() directly
                    # This means discriminated unions in output types won't get oneOf→anyOf conversion
                    # (but this is acceptable as output schemas are less critical than input schemas)
                    output_schema = TypeAdapter(model_out).json_schema()

                # FastMCP wraps union types in {"result": ...} for MCP protocol compatibility
                if is_union:
                    # Extract $defs from union schema (if present) and hoist to root level
                    defs = output_schema.pop("$defs", None)

                    # Wrap the union schema in an object with a "result" property
                    wrapped_schema = {
                        "type": "object",
                        "properties": {"result": output_schema},
                        "required": ["result"],
                        "x-fastmcp-wrap-result": True,
                    }

                    # Add $defs at root level so $ref pointers work correctly
                    if defs:
                        wrapped_schema["$defs"] = defs

                    output_schema = wrapped_schema

            # Create FunctionTool directly with our schema (bypasses FastMCP introspection)
            tool = FunctionTool(
                fn=wrapper,
                name=name or fn.__name__,
                title=title,
                description=description or inspect.getdoc(fn),
                parameters=input_schema,
                output_schema=output_schema,
                annotations=annotations,
                tags=tags or set(),
                enabled=True,
            )

            # Register the tool via add_tool() to trigger validation in mixins
            self.add_tool(tool)

            # Return the FunctionTool directly for programmatic access (bootstrap helpers, etc.)
            # The wrapper is already registered and accessible via the tool's fn attribute
            return cast(FunctionTool, tool)

        return outer

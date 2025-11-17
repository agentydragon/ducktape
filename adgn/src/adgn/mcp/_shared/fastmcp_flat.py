from __future__ import annotations

from collections.abc import Callable
import functools
import inspect
import logging
from typing import Annotated, Any, TypeVar, cast, get_args, get_origin, get_type_hints

from fastmcp.server import FastMCP
from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import PydanticUndefined

logger = logging.getLogger(__name__)

InputModelT = TypeVar("InputModelT", bound=BaseModel)
RegisterTool = Callable[[Callable[..., Any], dict[str, Any]], Callable[..., Any]]


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

        # Create the model instance
        payload = self._mcp_flat_input_model(**payload_kwargs)

        # Get context value if needed
        ctx_value = kwargs.get(self._context_name) if self._context_name else None

        return payload, ctx_value

    def __call__(self, **kwargs: Any) -> Any:
        """Execute the wrapped function with flattened parameters (sync version)."""
        if self._is_async:
            raise RuntimeError("Cannot call async function synchronously. Use await or async context.")

        payload, ctx_value = self._prepare_call(kwargs)

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


def _extract_signature_params(fn: Callable[..., Any]) -> tuple[inspect.Parameter, inspect.Parameter | None, str | None]:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if len(params) not in (1, 2):
        raise TypeError("@mcp_flat_model expects the payload model parameter and optional Context")
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


def _flat_model_decorator(
    register_tool: RegisterTool,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    annotations: Any | None = None,
    structured_output: bool = True,
    output_model: type[BaseModel] | None = None,
):
    def outer(fn: Callable[[InputModelT], Any]) -> Callable[[InputModelT], Any]:
        if not inspect.isfunction(fn):
            raise TypeError("@mcp_flat_model requires a plain function (not a callable object)")

        payload_param, context_param, context_name = _extract_signature_params(fn)
        hints = _collect_type_hints(fn)
        globs = fn.__globals__

        model_in = _resolve_base_model(
            name=payload_param.name,
            annotation=payload_param.annotation,
            hints=hints,
            globals_ns=globs,
            error_prefix="Parameter",
        )

        sig = inspect.signature(fn)
        inferred_return = output_model or hints.get("return", sig.return_annotation)
        model_out = inferred_return
        if isinstance(model_out, str):
            try:
                model_out = globs[model_out]
            except Exception as exc:  # pragma: no cover - mirrors earlier behaviour
                raise NotImplementedError(
                    "mcp_flat_model requires real types for output; string annotations not resolved. "
                    "Move models to module scope or pass output_model=... explicitly."
                ) from exc
        if structured_output and model_out is inspect.Signature.empty:
            raise TypeError("Return annotation is required when structured_output=True (or pass output_model=...)")

        _ensure_model_rebuild(model_in, kind="Input")

        rt = model_out
        if get_origin(rt) is Annotated:
            rt = get_args(rt)[0]
        if isinstance(rt, type) and issubclass(rt, BaseModel):
            _ensure_model_rebuild(rt, kind="Output")

        wrapper = _build_flat_wrapper(
            fn, model_in=model_in, model_out=model_out, context_param=context_param, context_name=context_name
        )
        _apply_wrapper_metadata(wrapper, model_in=model_in, model_out=model_out, context_param=context_param)

        inferred_desc = description or inspect.getdoc(fn) or None

        tool_kwargs = {
            "name": (name or fn.__name__ or None),
            "title": title,
            "description": inferred_desc,
            "annotations": annotations,
        }
        registered = register_tool(wrapper, tool_kwargs)
        return cast(Callable[[InputModelT], Any], registered)

    return outer


class FlatModelToolMixin:
    """Mixin that extends FastMCP.tool() with flat-model support."""

    class _ToolOpts(BaseModel):
        name: str | None = None
        title: str | None = None
        description: str | None = None
        annotations: Any | None = None
        structured_output: bool = True
        model_config = ConfigDict(extra="ignore")

    def tool(self, *args: Any, flat: bool = False, flat_output_model: type[BaseModel] | None = None, **kwargs: Any):  # type: ignore[misc]
        """Wrapper around FastMCP.tool with optional flat-model support.

        Args:
            flat: If True, enable flat-model mode
            flat_output_model: Optional explicit output model type for flat mode
            **kwargs: Other tool arguments passed through to FastMCP.tool
        """
        wants_flat = flat or (flat_output_model is not None)
        if not wants_flat:
            base_tool = super().tool  # type: ignore[misc]
            return base_tool(*args, **kwargs)

        opts = self._ToolOpts.model_validate(kwargs or {})

        def _register(fn: Callable[..., Any], mcp_tool_kwargs: dict[str, Any]) -> Callable[..., Any]:
            # Only pass kwargs accepted by FastMCP.tool overload (drop unsupported ones)
            base_tool = super(FlatModelToolMixin, self).tool  # type: ignore[misc]
            filtered = {k: v for k, v in mcp_tool_kwargs.items() if v is not None}
            decorator = base_tool(**filtered)
            decorator(fn)  # register the wrapper
            return fn

        return cast(
            Callable[[Callable[..., Any]], Callable[..., Any]],
            _flat_model_decorator(
                _register,
                name=opts.name,
                title=opts.title,
                description=opts.description,
                annotations=opts.annotations,
                structured_output=opts.structured_output,
                output_model=flat_output_model,
            ),
        )


class FlatModelFastMCP(FlatModelToolMixin, FastMCP):
    """FastMCP subclass with the flat-model convenience decorator built-in."""


def mcp_flat_model(
    mcp: FastMCP,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    annotations: Any | None = None,
    structured_output: bool = True,
    output_model: type[BaseModel] | None = None,
):
    if isinstance(mcp, FlatModelToolMixin):
        mixin = cast(FlatModelToolMixin, mcp)
        return cast(
            Callable[[Callable[..., Any]], Callable[..., Any]],
            mixin.tool(
                name=name,
                title=title,
                description=description,
                annotations=annotations,
                structured_output=structured_output,
                flat=True,
                flat_output_model=output_model,
            ),
        )

    def _register(fn: Callable[..., Any], mcp_tool_kwargs: dict[str, Any]) -> Callable[..., Any]:
        decorator = cast(Callable[[Callable[..., Any]], Callable[..., Any]], mcp.tool(**mcp_tool_kwargs))
        decorator(fn)  # register the wrapper
        return fn

    return cast(
        Callable[[Callable[..., Any]], Callable[..., Any]],
        _flat_model_decorator(
            _register,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            structured_output=structured_output,
            output_model=output_model,
        ),
    )

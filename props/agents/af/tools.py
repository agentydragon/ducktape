"""Adapt props' single-Pydantic-arg tool functions into MAF `FunctionTool`s.

props tools are `def name(args: ArgsModel) -> str | BaseModel` (sync or async) with the
docstring as the description. MAF invokes the wrapped function with the schema fields as
keyword arguments, so we rebuild the args model and delegate — keeping every tool body and
its flat JSON schema unchanged. Tool exceptions are caught by MAF's function-invocation loop
and returned to the model (enable `include_detailed_errors` on the client), matching props'
prior error-as-tool-result behavior.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from agent_framework import FunctionTool
from pydantic import BaseModel


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def direct_tool(fn: Callable[..., Any]) -> FunctionTool:
    """Wrap a props tool function (single Pydantic-model arg, or zero args) as a `FunctionTool`."""
    name = fn.__name__
    description = inspect.getdoc(fn) or name
    params = list(inspect.signature(fn).parameters.values())

    if not params:

        async def call_noargs() -> Any:
            return await _maybe_await(fn())

        call_noargs.__name__ = name
        return FunctionTool(func=call_noargs, name=name, description=description)

    args_model = params[0].annotation
    if not (inspect.isclass(args_model) and issubclass(args_model, BaseModel)):
        raise TypeError(f"direct_tool expects a single Pydantic-model parameter; {name} takes {args_model!r}")

    async def call_with_model(**fields: Any) -> Any:
        return await _maybe_await(fn(args_model(**fields)))

    call_with_model.__name__ = name
    return FunctionTool(func=call_with_model, name=name, description=description, input_model=args_model)


def direct_tools(*fns: Callable[..., Any]) -> list[FunctionTool]:
    """Wrap several props tool functions; preserves order."""
    return [direct_tool(fn) for fn in fns]

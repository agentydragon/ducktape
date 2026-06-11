"""Adapt props' single-Pydantic-arg tool functions into MAF `FunctionTool`s.

props tools are `def name(args: ArgsModel) -> str | BaseModel` (sync or async) with the
docstring as the description — one Pydantic-model parameter, not the per-parameter functions
MAF builds tools from natively. We bridge that with MAF's **schema-supplied `FunctionTool`**
mode: pass the model's JSON schema as `input_model` and reconstruct the model in a thin
`**fields` wrapper. This is deliberate over MAF's `input_model=<model>` mode, which validates
and *coerces* JSON-native args (e.g. a UUID string → `uuid.UUID`) and then rejects them against
the string schema ("expected string, got UUID"); the schema-supplied mode only checks the raw
args, so non-JSON-native fields (UUID/datetime) work. Tool exceptions are caught by MAF's
function loop and returned to the model (`include_detailed_errors` on the client), matching
props' prior error-as-tool-result behavior.
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
    # eval_str resolves string annotations (PEP 563 / `from __future__ import annotations`) to
    # the actual classes so the single Pydantic-model parameter can be detected.
    params = list(inspect.signature(fn, eval_str=True).parameters.values())

    if not params:

        async def call_noargs() -> Any:
            return await _maybe_await(fn())

        call_noargs.__name__ = name
        return FunctionTool(func=call_noargs, name=name, description=description)

    args_model = params[0].annotation
    if not (inspect.isclass(args_model) and issubclass(args_model, BaseModel)):
        raise TypeError(f"direct_tool expects a single Pydantic-model parameter; {name} takes {args_model!r}")

    # Pass the JSON schema (not the model) so MAF type-checks the model's raw JSON args against it.
    # Validating into the Pydantic model here (rather than letting MAF do it) avoids MAF coercing
    # JSON-native fields to Python types (e.g. a UUID string → uuid.UUID) and then rejecting them
    # against the string schema — which breaks every tool with a UUID/datetime/etc. field.
    schema = args_model.model_json_schema()

    async def call_with_model(**fields: Any) -> Any:
        return await _maybe_await(fn(args_model.model_validate(fields)))

    call_with_model.__name__ = name
    return FunctionTool(func=call_with_model, name=name, description=description, input_model=schema)


def direct_tools(*fns: Callable[..., Any]) -> list[FunctionTool]:
    """Wrap several props tool functions; preserves order."""
    return [direct_tool(fn) for fn in fns]

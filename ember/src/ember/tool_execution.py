from __future__ import annotations

from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass
import inspect
from typing import Any, Callable, Union, get_type_hints

from openai.types.responses import FunctionToolParam, ResponseFunctionToolCall
from pydantic import BaseModel

ToolPayload = Union[BaseModel, str]
ToolHandler = Callable[[BaseModel], Awaitable[ToolPayload]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    strict: bool = False

    @property
    def input_model(self) -> type[BaseModel]:
        return _first_handler_arg(self.handler)

    def to_param(self) -> FunctionToolParam:
        return FunctionToolParam(
            name=self.name,
            type="function",
            description=self.description,
            parameters=_json_schema_from_model(self.input_model),
            strict=self.strict,
        )


def execute_tool(tool_call: ResponseFunctionToolCall, specs: Mapping[str, ToolSpec]) -> Awaitable[ToolPayload]:
    spec = specs.get(tool_call.name)
    if spec is None:
        raise RuntimeError(f"Unknown tool {tool_call.name}")
    args = spec.input_model.model_validate_json(tool_call.arguments or "{}")
    return spec.handler(args)


def tool_params(specs: Iterable[ToolSpec]) -> list[FunctionToolParam]:
    return [spec.to_param() for spec in specs]


def _json_schema_from_model(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    parameters: dict[str, Any] = {"type": schema.get("type", "object"), "properties": schema.get("properties", {})}
    required = schema.get("required")
    if required:
        parameters["required"] = required
    return parameters


def _first_handler_arg(handler: ToolHandler) -> type[BaseModel]:
    sig = inspect.signature(handler)
    params = list(sig.parameters.values())
    if not params:
        raise RuntimeError("Tool handler must accept at least one argument")

    hints = get_type_hints(handler)
    first_param = params[0]
    annotation = hints.get(first_param.name, first_param.annotation)
    if annotation is first_param.empty or not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
        raise RuntimeError("Tool handler argument must be a Pydantic BaseModel subclass")
    return annotation

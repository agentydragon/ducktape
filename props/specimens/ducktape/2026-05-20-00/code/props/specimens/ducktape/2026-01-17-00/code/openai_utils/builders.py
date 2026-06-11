from __future__ import annotations

# ruff: noqa: E402

"""Adapter-level item builders.

Provides helpers to construct adapter items (tool calls, assistant text, reasoning).
Note: In production, reasoning items originate from the model. The make_item_reasoning
method is provided for test mocks that need to synthesize model responses.
"""

from typing import Any

import pydantic_core
from pydantic import BaseModel

from .model import AssistantMessageOut, FunctionCallItem, OutputText, ReasoningItem


def make_item_tool_call(*, call_id: str, name: str, arguments: dict[str, Any] | BaseModel) -> FunctionCallItem:
    """Create a function call item with JSON-serialized arguments."""
    args_dict = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
    return FunctionCallItem(
        call_id=call_id, name=name, arguments=pydantic_core.to_json(args_dict, fallback=str).decode("utf-8")
    )


def make_item_assistant_text(text: str) -> AssistantMessageOut:
    return AssistantMessageOut(parts=[OutputText(text=text)])


class ItemFactory:
    """Helper for constructing adapter items with convenient ID handling."""

    def __init__(self, call_id_prefix: str = "bootstrap") -> None:
        self._call_id_seq = 0
        self._reasoning_seq = 0
        self._prefix = call_id_prefix

    def next_call_id(self) -> str:
        self._call_id_seq += 1
        return f"{self._prefix}:{self._call_id_seq}"

    def _next_reasoning_id(self) -> int:
        self._reasoning_seq += 1
        return self._reasoning_seq

    def tool_call(
        self, name: str, arguments: dict[str, Any] | BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        cid = call_id or self.next_call_id()
        return make_item_tool_call(call_id=cid, name=name, arguments=arguments)

    def assistant_text(self, text: str) -> AssistantMessageOut:
        return make_item_assistant_text(text)

    def make_item_reasoning(self, id: str | None = None) -> ReasoningItem:
        return ReasoningItem(id=id or f"rs_{self._next_reasoning_id()}")

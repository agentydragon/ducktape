from __future__ import annotations

# ruff: noqa: E402

"""Adapter-level item builders for production code.

These helpers construct only adapter items that production code is allowed to
create directly (not full ResponsesResult objects, and not reasoning items).
Reasoning items originate from the model and should never be synthesized in prod.
"""

import json
from typing import Any

from .model import AssistantMessageOut, FunctionCallItem, FunctionCallOutputItem, OutputText


def make_item_tool_call(*, call_id: str, name: str, arguments: dict[str, Any]) -> FunctionCallItem:
    """Create a function call item with JSON-serialized arguments."""
    return FunctionCallItem(call_id=call_id, name=name, arguments=json.dumps(arguments))


def make_item_assistant_text(text: str) -> AssistantMessageOut:
    return AssistantMessageOut(parts=[OutputText(text=text)])


class ItemFactory:
    """Small helper for constructing adapter items with convenient call_id handling.

    Production-safe: creates only tool_call and assistant_text items. It does not
    fabricate reasoning items or full ResponsesResult objects.
    """

    def __init__(self, call_id_prefix: str = "bootstrap") -> None:
        self._i = 0
        self._prefix = call_id_prefix

    def next_call_id(self) -> str:
        self._i += 1
        return f"{self._prefix}:{self._i}"

    def tool_call(self, name: str, arguments: dict[str, Any], call_id: str | None = None) -> FunctionCallItem:
        cid = call_id or self.next_call_id()
        return make_item_tool_call(call_id=cid, name=name, arguments=arguments)

    def assistant_text(self, text: str) -> AssistantMessageOut:
        return make_item_assistant_text(text)

    def tool_call_with_output(
        self,
        name: str,
        arguments: dict[str, Any],
        output: str | dict[str, Any] | FunctionCallOutputItem,
        call_id: str | None = None,
    ) -> tuple[FunctionCallItem, FunctionCallOutputItem]:
        call = self.tool_call(name, arguments, call_id)
        if isinstance(output, FunctionCallOutputItem):
            if output.call_id == call.call_id:
                out = output
            else:  # keep payload but align call_id
                out = FunctionCallOutputItem(call_id=call.call_id, output=output.output)
        else:
            if isinstance(output, str):
                out_str = output
            else:
                try:
                    out_str = json.dumps(output)
                except TypeError:
                    out_str = str(output)
            # Ensure call_id is present for type-checkers
            assert call.call_id is not None
            out = FunctionCallOutputItem(call_id=call.call_id, output=out_str)
        return call, out

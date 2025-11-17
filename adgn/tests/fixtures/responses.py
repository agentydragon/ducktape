from __future__ import annotations

import os
from typing import Any

from fastmcp.client.client import CallToolResult
from pydantic import TypeAdapter
import pytest
from tests.llm.support.openai_mock import LIVE, make_mock

from adgn.openai_utils import builders
from adgn.openai_utils.model import (
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    ReasoningItem,
    ResponseOutItem,
    ResponsesResult,
    Usage,
)


@pytest.fixture(scope="session")
def reasoning_model() -> str:
    """Default reasoning-capable model for adapter fixtures.

    Tests may override via RESPONSES_TEST_MODEL env.
    """
    return os.environ.get("RESPONSES_TEST_MODEL", "gpt-5-nano")


class ResponsesFactory:
    """Convenience adapter response builders bound to a model name."""

    def __init__(self, model: str):
        self.model = model
        self._item_factory = builders.ItemFactory(call_id_prefix="test")
        self._reasoning_seq = 0

    def _next_reasoning_id(self) -> int:
        self._reasoning_seq += 1
        return self._reasoning_seq

    def make_assistant_message(self, text: str) -> ResponsesResult:
        return ResponsesResult(
            id="resp_msg",
            usage=Usage(input_tokens=0, output_tokens=1, total_tokens=1),
            output=[self._item_factory.assistant_text(text)],
        )

    def make_tool_call(self, name: str, arguments: dict[str, Any], call_id: str | None = None) -> ResponsesResult:
        return self.make(self._item_factory.tool_call(name, arguments, call_id))

    # ---- Low-level item builders (compose with make(...items)) ----

    def assistant_text(self, text: str) -> AssistantMessageOut:
        """Create an assistant text item. Delegates to ItemFactory."""
        return self._item_factory.assistant_text(text)

    def tool_call(self, name: str, arguments: dict[str, Any], call_id: str | None = None) -> FunctionCallItem:
        """Create a tool call item. Delegates to ItemFactory."""
        return self._item_factory.tool_call(name, arguments, call_id)

    def make_item_reasoning(self, id: str | None = None) -> ReasoningItem:
        return ReasoningItem(id=id or f"rs_{self._next_reasoning_id()}")

    def make_item_tool_call_auto(self, name: str, arguments: dict[str, Any]) -> FunctionCallItem:
        return self._item_factory.tool_call(name, arguments)

    # ---- Message/response constructors (compose items) ----

    def make(self, *items: ResponseOutItem) -> ResponsesResult:
        # Minimal usage heuristic: count assistant text parts as output tokens >=1
        out_tokens = 0
        for it in items:
            if isinstance(it, AssistantMessageOut):
                out_tokens += max(1, len(it.text))
        usage = Usage(input_tokens=0, output_tokens=(1 if out_tokens else 0), total_tokens=(1 if out_tokens else 0))
        # Coerce any plain dicts to proper models if needed (not expected here)
        return ResponsesResult(id="resp_generic", usage=usage, output=list(items))

    def make_tool_call_auto(self, name: str, arguments: dict[str, Any]) -> ResponsesResult:
        return self.make(self.make_item_tool_call_auto(name, arguments))

    def make_final_assistant(self, text: str) -> ResponsesResult:
        return self.make(self._item_factory.assistant_text(text))

    def make_reasoning_then_assistant(self, text: str) -> ResponsesResult:
        return self.make(self.make_item_reasoning(), self._item_factory.assistant_text(text))

    def make_reasoning_tool_then_assistant(
        self, *, call_id: str, name: str, arguments: dict[str, Any], text: str
    ) -> ResponsesResult:
        return self.make(
            self.make_item_reasoning(), self._item_factory.tool_call(name, arguments, call_id), self._item_factory.assistant_text(text)
        )

    def make_tool_call_with_output(
        self, name: str, arguments: dict[str, Any], output: Any, call_id: str | None = None
    ) -> ResponsesResult:
        call = self._item_factory.tool_call(name, arguments, call_id)
        result = CallToolResult(content=[], structured_content=output, data=None, is_error=False)
        payload_json = TypeAdapter(CallToolResult).dump_json(result, by_alias=True)
        out = FunctionCallOutputItem(call_id=call.call_id, output=payload_json.decode('utf-8'))
        return self.make(call, out)


@pytest.fixture(scope="session")
def responses_factory(reasoning_model: str) -> ResponsesFactory:
    return ResponsesFactory(reasoning_model)


@pytest.fixture
def openai_client_param(request, live_openai):
    """Parametrized OpenAI client fixture for tests.

    Usage (indirect): parametrize with either a behavior function or LIVE sentinel:
        @pytest.mark.parametrize("openai_client_param", [behavior_fn, LIVE], indirect=True)

    - If parameter is LIVE, returns the live_openai fixture (AsyncOpenAI or skip if not set).
    - Otherwise, assumes a behavior function(req) -> ResponsesResult and returns a mock client.
    """
    param = getattr(request, "param", None)
    if param is LIVE:
        return live_openai
    if callable(param):
        return make_mock(param)
    raise pytest.SkipTest("openai_client_param requires a behavior function or LIVE sentinel")

"""Tests for ScriptHandler and ScriptBuilder."""

from __future__ import annotations

import json

import pytest
import pytest_bazel
from pydantic import BaseModel

from agent_core.events import ToolCallOutput
from agent_core.loop_control import InjectItems, NoAction
from agent_core.script_handler import (
    ScriptBuilder,
    ScriptError,
    ScriptEvent,
    ScriptGen,
    ScriptHandler,
    find_tool_result,
    find_tool_result_typed,
    script_handler,
)
from agent_core.tool_provider import ToolResult
from mcp_infra.exec.models import BaseExecResult, Exited
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import FunctionCallItem, SystemMessage, UserMessage

TEST_PREFIX = MCPMountPrefix("test")

EXEC_OK = BaseExecResult(exit=Exited(exit_code=0), stdout="", stderr="", duration_ms=0)


class Payload(BaseModel):
    """Reusable test payload model."""

    v: str


def _tool_result_event(call_id: str, *, structured: dict | None = None, is_error: bool = False) -> ToolCallOutput:
    return ToolCallOutput(call_id=call_id, result=ToolResult(structured_content=structured, is_error=is_error))


def test_prime_yield_must_be_none():
    @script_handler
    def bad_script() -> ScriptGen:
        yield [UserMessage.text("oops")]

    with pytest.raises(RuntimeError, match="first yield must be None"):
        bad_script()


def test_single_call_injection():
    @script_handler
    def script() -> ScriptGen:
        yield None
        yield [FunctionCallItem(call_id="c1", name="test_tool", arguments="{}")]

    handler = script()
    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    assert len(decision.items) == 1
    assert isinstance(decision.items[0], FunctionCallItem)
    assert decision.items[0].call_id == "c1"


def test_generator_return_becomes_no_action():
    @script_handler
    def script() -> ScriptGen:
        yield None

    handler = script()
    assert isinstance(handler.on_before_sample(), NoAction)
    assert isinstance(handler.on_before_sample(), NoAction)


def test_generator_exception_propagates():
    @script_handler
    def script() -> ScriptGen:
        yield None
        raise ValueError("script failed")

    handler = script()
    with pytest.raises(ValueError, match="script failed"):
        handler.on_before_sample()


def test_multiple_serial_yields():
    @script_handler
    def script() -> ScriptGen:
        yield None
        events = yield [FunctionCallItem(call_id="c1", name="step1", arguments="{}")]
        assert len(events) == 1
        assert events[0].call_id == "c1"

        events = yield [FunctionCallItem(call_id="c2", name="step2", arguments="{}")]
        assert len(events) == 1
        assert events[0].call_id == "c2"

    handler = script()

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    handler.on_tool_result_event(_tool_result_event("c1", structured=EXEC_OK.model_dump()))

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    handler.on_tool_result_event(_tool_result_event("c2", structured=EXEC_OK.model_dump()))

    assert isinstance(handler.on_before_sample(), NoAction)


def test_parallel_calls():
    @script_handler
    def script() -> ScriptGen:
        yield None
        events = yield [
            FunctionCallItem(call_id="p1", name="tool_a", arguments="{}"),
            FunctionCallItem(call_id="p2", name="tool_b", arguments="{}"),
        ]
        assert len(events) == 2

    handler = script()

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    assert len(decision.items) == 2

    handler.on_tool_result_event(_tool_result_event("p1", structured=EXEC_OK.model_dump()))
    handler.on_tool_result_event(_tool_result_event("p2", structured=EXEC_OK.model_dump()))

    assert isinstance(handler.on_before_sample(), NoAction)


def test_yield_none_is_no_action():
    @script_handler
    def script() -> ScriptGen:
        yield None
        yield None  # no action
        yield [FunctionCallItem(call_id="c1", name="tool", arguments="{}")]

    handler = script()
    assert isinstance(handler.on_before_sample(), NoAction)
    assert isinstance(handler.on_before_sample(), InjectItems)


def test_events_not_buffered_after_exhaustion():
    @script_handler
    def script() -> ScriptGen:
        yield None

    handler = script()
    handler.on_before_sample()  # exhaust
    handler.on_tool_result_event(_tool_result_event("late", structured=EXEC_OK.model_dump()))
    assert isinstance(handler.on_before_sample(), NoAction)


def test_message_injection():
    @script_handler
    def script() -> ScriptGen:
        yield None
        yield [SystemMessage.text("system msg"), UserMessage.text("user msg")]

    handler = script()
    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    assert len(decision.items) == 2


def test_yield_from_sub_generator():
    def sub_step() -> ScriptGen[str]:
        events: list[ScriptEvent] = yield [FunctionCallItem(call_id="sub1", name="sub_tool", arguments="{}")]
        result = find_tool_result(events, "sub1")
        assert result.structured_content is not None
        return "sub_result"

    @script_handler
    def script() -> ScriptGen:
        yield None
        result = yield from sub_step()
        assert result == "sub_result"

    handler = script()

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)

    handler.on_tool_result_event(_tool_result_event("sub1", structured={"key": "val"}))

    assert isinstance(handler.on_before_sample(), NoAction)


def test_script_handler_decorator_with_args():
    """The @script_handler decorator wraps a generator function into a handler factory."""

    @script_handler
    def my_script(prefix: str) -> ScriptGen:
        yield None
        yield [FunctionCallItem(call_id=f"{prefix}:1", name="tool", arguments="{}")]

    handler = my_script("test")
    assert isinstance(handler, ScriptHandler)

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    first_item = decision.items[0]
    assert isinstance(first_item, FunctionCallItem)
    assert first_item.call_id == "test:1"


def test_find_tool_result_found():
    events = [_tool_result_event("a"), _tool_result_event("b")]
    result = find_tool_result(events, "b")
    assert result is not None


def test_find_tool_result_not_found():
    events = [_tool_result_event("a")]
    with pytest.raises(ValueError, match="No tool result"):
        find_tool_result(events, "missing")


def test_find_tool_result_duplicate_raises():
    events = [_tool_result_event("a"), _tool_result_event("a")]
    with pytest.raises(ValueError, match="Multiple tool results"):
        find_tool_result(events, "a")


class SampleOutput(BaseModel):
    value: str


def test_find_tool_result_typed_success():
    events = [_tool_result_event("c1", structured=SampleOutput(value="hello").model_dump())]
    out = find_tool_result_typed(events, "c1", SampleOutput)
    assert out.value == "hello"


def test_find_tool_result_typed_error():
    events = [_tool_result_event("c1", structured=SampleOutput(value="x").model_dump(), is_error=True)]
    with pytest.raises(ScriptError, match="returned error"):
        find_tool_result_typed(events, "c1", SampleOutput)


def test_find_tool_result_typed_no_structured_content():
    events = [_tool_result_event("c1")]
    with pytest.raises(ScriptError, match="no structured content"):
        find_tool_result_typed(events, "c1", SampleOutput)


def test_script_builder_call():
    b = ScriptBuilder()
    call = b.call(TEST_PREFIX, "my_tool", Payload(v="test"))
    assert call.name == "test_my_tool"
    assert call.call_id == "bootstrap:1"
    assert call.arguments is not None
    assert json.loads(call.arguments) == {"v": "test"}


def test_script_builder_auto_increment_ids():
    b = ScriptBuilder()
    c1 = b.call(TEST_PREFIX, "t", Payload(v="a"))
    c2 = b.call(TEST_PREFIX, "t", Payload(v="b"))
    c3 = b.call(TEST_PREFIX, "t", Payload(v="c"))
    assert c1.call_id == "bootstrap:1"
    assert c2.call_id == "bootstrap:2"
    assert c3.call_id == "bootstrap:3"


if __name__ == "__main__":
    pytest_bazel.main()

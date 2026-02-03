"""Tests for ScriptHandler and ScriptBuilder."""

from __future__ import annotations

import json

import pytest
import pytest_bazel
from pydantic import BaseModel

from agent_core.events import ToolCallOutput, TranscriptEvent
from agent_core.loop_control import InjectItems, NoAction
from agent_core.script_builder import ScriptBuilder, _validate_exit_zero
from agent_core.script_handler import ScriptError, ScriptGen, ScriptHandler, find_tool_result, find_tool_result_typed
from agent_core.tool_provider import ToolResult
from mcp_infra.exec.models import BaseExecResult, Exited, Killed
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import FunctionCallItem, SystemMessage, UserMessage

TEST_PREFIX = MCPMountPrefix("test")

EXEC_OK = BaseExecResult(exit=Exited(exit_code=0), stdout="", stderr="", duration_ms=0)


def _tool_result_event(call_id: str, *, structured: dict | None = None, is_error: bool = False) -> ToolCallOutput:
    return ToolCallOutput(call_id=call_id, result=ToolResult(structured_content=structured, is_error=is_error))


def _exec_result_event(call_id: str, result: BaseExecResult = EXEC_OK) -> ToolCallOutput:
    """Create a ToolCallOutput wrapping a BaseExecResult."""
    return _tool_result_event(call_id, structured=result.model_dump())


def test_prime_yield_must_be_none():
    def bad_script() -> ScriptGen:
        yield [UserMessage.text("oops")]

    with pytest.raises(RuntimeError, match="first yield must be None"):
        ScriptHandler(bad_script())


def test_single_call_injection():
    def script() -> ScriptGen:
        yield None
        yield [FunctionCallItem(call_id="c1", name="test_tool", arguments="{}")]

    handler = ScriptHandler(script())
    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    assert len(decision.items) == 1
    assert isinstance(decision.items[0], FunctionCallItem)
    assert decision.items[0].call_id == "c1"


def test_generator_return_becomes_no_action():
    def script() -> ScriptGen:
        yield None

    handler = ScriptHandler(script())
    assert isinstance(handler.on_before_sample(), NoAction)
    assert isinstance(handler.on_before_sample(), NoAction)


def test_generator_exception_propagates():
    def script() -> ScriptGen:
        yield None
        raise ValueError("script failed")

    handler = ScriptHandler(script())
    with pytest.raises(ValueError, match="script failed"):
        handler.on_before_sample()


def test_multiple_serial_yields():
    def script() -> ScriptGen:
        yield None
        events = yield [FunctionCallItem(call_id="c1", name="step1", arguments="{}")]
        assert len(events) == 1
        assert events[0].call_id == "c1"

        events = yield [FunctionCallItem(call_id="c2", name="step2", arguments="{}")]
        assert len(events) == 1
        assert events[0].call_id == "c2"

    handler = ScriptHandler(script())

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    handler.on_tool_result_event(_exec_result_event("c1"))

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    handler.on_tool_result_event(_exec_result_event("c2"))

    assert isinstance(handler.on_before_sample(), NoAction)


def test_parallel_calls():
    def script() -> ScriptGen:
        yield None
        events = yield [
            FunctionCallItem(call_id="p1", name="tool_a", arguments="{}"),
            FunctionCallItem(call_id="p2", name="tool_b", arguments="{}"),
        ]
        assert len(events) == 2

    handler = ScriptHandler(script())

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    assert len(decision.items) == 2

    handler.on_tool_result_event(_exec_result_event("p1"))
    handler.on_tool_result_event(_exec_result_event("p2"))

    assert isinstance(handler.on_before_sample(), NoAction)


def test_yield_none_is_no_action():
    def script() -> ScriptGen:
        yield None
        yield None  # no action
        yield [FunctionCallItem(call_id="c1", name="tool", arguments="{}")]

    handler = ScriptHandler(script())
    assert isinstance(handler.on_before_sample(), NoAction)
    assert isinstance(handler.on_before_sample(), InjectItems)


def test_events_not_buffered_after_exhaustion():
    def script() -> ScriptGen:
        yield None

    handler = ScriptHandler(script())
    handler.on_before_sample()  # exhaust
    handler.on_tool_result_event(_exec_result_event("late"))
    assert isinstance(handler.on_before_sample(), NoAction)


def test_message_injection():
    def script() -> ScriptGen:
        yield None
        yield [SystemMessage.text("system msg"), UserMessage.text("user msg")]

    handler = ScriptHandler(script())
    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)
    assert len(decision.items) == 2


def test_yield_from_sub_generator():
    def sub_step() -> ScriptGen[str]:
        events: list[TranscriptEvent] = yield [FunctionCallItem(call_id="sub1", name="sub_tool", arguments="{}")]
        result = find_tool_result(events, "sub1")
        assert result.structured_content is not None
        return "sub_result"

    def script() -> ScriptGen:
        yield None
        result = yield from sub_step()
        assert result == "sub_result"

    handler = ScriptHandler(script())

    decision = handler.on_before_sample()
    assert isinstance(decision, InjectItems)

    handler.on_tool_result_event(_tool_result_event("sub1", structured={"key": "val"}))

    assert isinstance(handler.on_before_sample(), NoAction)


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


def test_validate_exit_zero_success():
    _validate_exit_zero(EXEC_OK, ["echo", "hello"])


def test_validate_exit_zero_nonzero_exit():
    result = BaseExecResult(exit=Exited(exit_code=1), stdout="", stderr="", duration_ms=0)
    with pytest.raises(ScriptError, match="Command failed"):
        _validate_exit_zero(result, ["failing_cmd"])


def test_validate_exit_zero_killed():
    result = BaseExecResult(exit=Killed(signal=9), stdout="", stderr="", duration_ms=0)
    with pytest.raises(ScriptError, match="Command failed"):
        _validate_exit_zero(result, ["killed_cmd"])


def test_script_builder_call():
    b = ScriptBuilder()

    class Payload(BaseModel):
        x: int

    call = b.call(TEST_PREFIX, "my_tool", Payload(x=42))
    assert call.name == "test_my_tool"
    assert call.call_id == "bootstrap:1"
    assert call.arguments is not None
    assert json.loads(call.arguments) == {"x": 42}


def test_script_builder_auto_increment_ids():
    b = ScriptBuilder()

    class P(BaseModel):
        v: str

    c1 = b.call(TEST_PREFIX, "t", P(v="a"))
    c2 = b.call(TEST_PREFIX, "t", P(v="b"))
    c3 = b.call(TEST_PREFIX, "t", P(v="c"))
    assert c1.call_id == "bootstrap:1"
    assert c2.call_id == "bootstrap:2"
    assert c3.call_id == "bootstrap:3"


def test_script_builder_custom_prefix():
    b = ScriptBuilder(call_id_prefix="init")

    class P(BaseModel):
        v: str

    c = b.call(TEST_PREFIX, "t", P(v="x"))
    assert c.call_id == "init:1"


def test_script_builder_explicit_call_id():
    b = ScriptBuilder()

    class P(BaseModel):
        v: str

    c = b.call(TEST_PREFIX, "t", P(v="x"), call_id="custom-99")
    assert c.call_id == "custom-99"


if __name__ == "__main__":
    pytest_bazel.main()

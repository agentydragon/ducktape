"""Tests for tool execution: errors, parallel calls, malformed input, sanitization, and schema generation."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Annotated, Any, Final, Literal

import pytest
from hamcrest import all_of, assert_that, contains_string, greater_than_or_equal_to, has_entries, has_length
from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field

from agent_core.agent import Agent, _sanitize_mcp_result
from agent_core.events import ToolCall, ToolCallOutput
from agent_core.handler import BaseHandler, FinishOnTextMessageHandler
from agent_core.loop_control import Abort, AllowAnyToolOrTextMessage, InjectItems, RequireAnyTool
from agent_core_testing.fixtures import FAIL_TOOL_NAME
from agent_core_testing.matchers import assert_function_call_output_structured, tool_call_with_error_text
from agent_core_testing.openai_mock import NoopOpenAIClient, make_mock
from agent_core_testing.responses import DecoratorMock, ResponsesFactory
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.simple_servers import SendMessageInput
from openai_utils.model import FunctionCallItem, ResponsesRequest, ResponsesResult, SystemMessage, UserMessage
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# --- Tool error continuation tests ---


async def test_tool_error_continues_turn(compositor, compositor_client, validation_server, recording_handler) -> None:
    """Test that a tool validation error doesn't abort the turn.

    The agent should:
    1. Call the tool with wrong mime type (text/plain)
    2. Get a validation error
    3. Continue to the next phase
    4. Retry with correct mime type (text/markdown)
    5. Successfully complete
    """
    mounted = await compositor.mount_inproc(MCPMountPrefix("validator"), validation_server)
    tool_name = build_mcp_function(mounted.prefix, mounted.server.send_message_tool.name)

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        yield
        # First attempt with wrong mime type
        yield m.tool_call(tool_name, SendMessageInput(mime="text/plain", content="Hello"))
        # After error, agent retries with correct mime type
        yield m.tool_call(tool_name, SendMessageInput(mime="text/markdown", content="Hello"))
        yield m.assistant_text("Successfully sent message")

    agent = await Agent.create(
        mcp_client=compositor_client,
        client=mock,
        handlers=[FinishOnTextMessageHandler(), recording_handler],
        tool_policy=RequireAnyTool(),
    )
    agent.process_message(UserMessage.text("send message"))

    result = await agent.run()

    # Verify the sequence of events
    tool_calls = [evt for evt in recording_handler.records if evt.type == "tool_call"]
    outputs = [evt for evt in recording_handler.records if evt.type == "function_call_output"]

    assert len(tool_calls) == 2, f"Expected 2 tool calls, got {len(tool_calls)}"

    # First call should fail with validation error
    first_output = outputs[0]
    assert first_output.result.isError is True
    error_content = first_output.result.content[0].text
    assert_that(error_content.lower(), contains_string("error"))
    assert "text/markdown" in error_content or "literal" in error_content.lower()

    # Second call should succeed
    second_output = outputs[1]
    assert second_output.result.isError is False
    assert_function_call_output_structured([second_output], has_entries(ok=True))

    assert_that(result.text, contains_string("Successfully sent message"))


# --- Tool error sequence tests ---


class FailInput(OpenAIStrictModeBaseModel):
    """Input for editor/fail tool (test fixture)."""

    x: int


async def test_app_level_error_payload_surfaced_in_structured_content(
    compositor, compositor_client, error_payload_server, recording_handler
) -> None:
    """Test that application-level error payloads are surfaced in structuredContent.

    Note: This tests the {"ok": False, "error": "..."} pattern in structuredContent,
    NOT the MCP-level isError flag. For MCP-level error testing, see test_tool_error_continues_turn.
    """
    mounted = await compositor.mount_inproc(MCPMountPrefix("editor"), error_payload_server)

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        yield
        yield m.tool_call(build_mcp_function(mounted.prefix, FAIL_TOOL_NAME), FailInput(x=1))
        yield m.assistant_text("done")

    agent = await Agent.create(
        mcp_client=compositor_client,
        client=mock,
        handlers=[FinishOnTextMessageHandler(), recording_handler],
        tool_policy=AllowAnyToolOrTextMessage(),
    )
    agent.process_message(UserMessage.text("fail"))
    await agent.run()

    assert_function_call_output_structured(recording_handler.records, has_entries(ok=False, error="boom"))


# --- Parallel tool call tests ---


class SlowInput(BaseModel):
    """Empty input for slow() tool."""


class Slow2Input(BaseModel):
    """Empty input for slow2() tool."""


class OneShotSyntheticHandler(BaseHandler):
    """Handler that injects synthetic output once, then aborts."""

    def __init__(self, outputs: list[Any]):
        self._done = False
        self._outputs = outputs

    def on_before_sample(self):
        if not self._done:
            self._done = True
            return InjectItems(items=tuple(self._outputs))
        return Abort()


async def test_parallel_tool_calls_reduce_wall_time(
    compositor, compositor_client, slow_server, recording_handler, responses_factory
):
    # Two tool calls with ~0.30s latency each; if run in parallel, wall time ~0.30-0.45s
    # Mount slow server and capture Mounted object
    mounted_slow = await compositor.mount_inproc(MCPMountPrefix("dummy"), slow_server)

    tc1 = responses_factory.mcp_tool_call(mounted_slow.prefix, "slow", SlowInput())
    tc2 = responses_factory.mcp_tool_call(mounted_slow.prefix, "slow2", Slow2Input())

    handler = OneShotSyntheticHandler(outputs=[tc1, tc2])

    agent = await Agent.create(
        mcp_client=compositor_client,
        client=NoopOpenAIClient(),  # SyntheticAction path bypasses OpenAI
        parallel_tool_calls=True,
        handlers=[handler, recording_handler],
        tool_policy=RequireAnyTool(),
    )
    agent.process_message(UserMessage.text("go"))

    t0 = time.perf_counter()
    await agent.run()
    elapsed = time.perf_counter() - t0

    # Assert shorter than serial (~0.60s), with generous headroom for CI noise
    # Threshold tuned for CI noise; serial takes ~0.60s, expect faster here
    assert elapsed < 0.55, f"expected parallel speedup; took {elapsed:.3f}s"

    # Sanity checks on outputs/metrics via recording handler
    tool_calls = [e for e in recording_handler.records if isinstance(e, ToolCall)]
    tool_outputs = [e for e in recording_handler.records if isinstance(e, ToolCallOutput)]
    assert_that(tool_calls, has_length(greater_than_or_equal_to(2)))
    assert_that(tool_outputs, has_length(greater_than_or_equal_to(2)))


# --- Malformed JSON tests ---


async def _run_malformed_json_test(
    mcp_client_echo,
    recording_handler,
    make_first_turn: Callable[[ResponsesFactory], ResponsesResult],
    parallel: bool = False,
) -> tuple[str, list]:
    """Helper to run malformed JSON tests with custom first turn."""
    factory = ResponsesFactory("test-model")

    async def handle_request(req: ResponsesRequest) -> ResponsesResult:
        if len(req.input) == 1:
            return make_first_turn(factory)
        return factory.make_assistant_message("I received an error")

    client = make_mock(handle_request)
    agent = await Agent.create(
        mcp_client=mcp_client_echo,
        client=client,
        handlers=[FinishOnTextMessageHandler(), recording_handler],
        parallel_tool_calls=parallel,
        tool_policy=RequireAnyTool(),
    )
    agent.process_message(UserMessage.text("use echo"))

    res = await agent.run()
    events = recording_handler.records
    return res.text, events


async def test_malformed_json_in_tool_arguments(mcp_client_echo, recording_handler) -> None:
    """Test that malformed JSON in tool arguments is converted to error tool result."""

    def make_turn(factory: ResponsesFactory) -> ResponsesResult:
        malformed_call = FunctionCallItem(
            type="function_call",
            name="echo_echo",
            arguments='{"text": "unterminated string',  # Malformed JSON
            call_id="test:1",
        )
        return factory.make(malformed_call)

    text, events = await _run_malformed_json_test(mcp_client_echo, recording_handler, make_turn)

    # Agent should complete successfully despite malformed JSON
    assert "error" in text.lower() or "invalid" in text.lower()

    # Check that error was emitted as a tool result
    tool_outputs = [evt for evt in events if isinstance(evt, ToolCallOutput)]
    assert len(tool_outputs) == 1

    tool_result = tool_outputs[0].result
    assert_that(
        tool_result,
        tool_call_with_error_text(all_of(contains_string("Invalid JSON"), contains_string("unterminated string"))),
    )


async def test_non_dict_json_in_tool_arguments(mcp_client_echo, recording_handler) -> None:
    """Test that non-dict JSON (like array) in tool arguments is converted to error."""

    def make_turn(factory: ResponsesFactory) -> ResponsesResult:
        non_dict_call = FunctionCallItem(
            type="function_call",
            name="echo_echo",
            arguments='["not", "an", "object"]',  # Valid JSON but not a dict
            call_id="test:1",
        )
        return factory.make(non_dict_call)

    text, events = await _run_malformed_json_test(mcp_client_echo, recording_handler, make_turn)

    # Agent should complete successfully
    assert "error" in text.lower()

    # Check that error was emitted as a tool result
    tool_outputs = [evt for evt in events if isinstance(evt, ToolCallOutput)]
    assert len(tool_outputs) == 1

    tool_result = tool_outputs[0].result
    assert_that(tool_result, tool_call_with_error_text(contains_string("must be a JSON object")))


async def test_malformed_json_parallel_tool_calls(mcp_client_echo, recording_handler) -> None:
    """Test malformed JSON handling with parallel tool calls enabled."""

    def make_turn(factory: ResponsesFactory) -> ResponsesResult:
        good_call = FunctionCallItem(
            type="function_call", name="echo_echo", arguments='{"text": "good call"}', call_id="test:1"
        )
        bad_call = FunctionCallItem(
            type="function_call",
            name="echo_echo",
            arguments='{"text": "bad',  # Malformed
            call_id="test:2",
        )
        return factory.make(good_call, bad_call)

    text, events = await _run_malformed_json_test(mcp_client_echo, recording_handler, make_turn, parallel=True)

    # Agent should complete successfully
    assert text

    # Check that we got two tool results: one success, one error
    tool_outputs = [evt for evt in events if isinstance(evt, ToolCallOutput)]
    assert len(tool_outputs) == 2

    # One should be error, one should be success
    results = [out.result for out in tool_outputs]
    error_count = sum(1 for r in results if r.isError)
    success_count = sum(1 for r in results if not r.isError)

    assert error_count == 1
    assert success_count == 1


# --- Null byte sanitization tests ---


def test_no_nulls_unchanged(text_content):
    result = mcp_types.CallToolResult(content=[text_content("clean")], isError=False)
    sanitized = _sanitize_mcp_result(result)
    assert sanitized.content == result.content


def test_nulls_in_text(text_content):
    result = mcp_types.CallToolResult(content=[text_content("a\x00b\x00c")], isError=False)
    sanitized = _sanitize_mcp_result(result)
    first_item = sanitized.content[0]
    assert isinstance(first_item, mcp_types.TextContent)
    text = first_item.text
    assert text.startswith("NOTE: 2 null byte(s) removed")
    assert "abc" in text


def test_nulls_in_structured(text_content):
    result = mcp_types.CallToolResult(
        content=[text_content("output")], structuredContent={"k": "v\x00", "nested": {"d": "x\x00"}}, isError=False
    )
    sanitized = _sanitize_mcp_result(result)
    assert sanitized.structuredContent == {"k": "v", "nested": {"d": "x"}}
    first_item = sanitized.content[0]
    assert isinstance(first_item, mcp_types.TextContent)
    assert "NOTE: 2 null byte(s) removed" in first_item.text


def test_empty_content_nulls_in_structured():
    result = mcp_types.CallToolResult(content=[], structuredContent={"a": "b\x00"}, isError=False)
    sanitized = _sanitize_mcp_result(result)
    assert len(sanitized.content) == 1
    first_item = sanitized.content[0]
    assert isinstance(first_item, mcp_types.TextContent)
    assert first_item.text.startswith("NOTE: 1 null byte(s) removed")


def test_prepends_to_first_text_block(text_content):
    result = mcp_types.CallToolResult(content=[text_content("a\x00"), text_content("b\x00")], isError=False)
    sanitized = _sanitize_mcp_result(result)
    first_item = sanitized.content[0]
    second_item = sanitized.content[1]
    assert isinstance(first_item, mcp_types.TextContent)
    assert isinstance(second_item, mcp_types.TextContent)
    assert first_item.text.startswith("NOTE: 2 null byte(s) removed")
    assert second_item.text == "b"


def test_inserts_before_non_text_first_block(text_content):
    result = mcp_types.CallToolResult(
        content=[mcp_types.ImageContent(type="image", data="data", mimeType="image/png"), text_content("a\x00")],
        isError=False,
    )
    sanitized = _sanitize_mcp_result(result)
    assert len(sanitized.content) == 3
    first_item = sanitized.content[0]
    assert isinstance(first_item, mcp_types.TextContent)
    assert first_item.text.startswith("NOTE: 1 null byte(s) removed")
    assert isinstance(sanitized.content[1], mcp_types.ImageContent)


def test_nested_structures(text_content):
    result = mcp_types.CallToolResult(
        content=[text_content("x")], structuredContent={"a": {"b": {"c": ["x\x00", "y\x00"]}}}, isError=False
    )
    sanitized = _sanitize_mcp_result(result)
    assert sanitized.structuredContent == {"a": {"b": {"c": ["x", "y"]}}}


def test_preserves_error_and_meta(text_content):
    result = mcp_types.CallToolResult(content=[text_content("err\x00")], isError=True, _meta={"k": "v"})
    sanitized = _sanitize_mcp_result(result)
    assert sanitized.isError is True
    assert sanitized.meta == {"k": "v"}


# --- Flat tool schema tests ---

TOOL_A_NAME: Final[str] = "tool_a"


class ToolAInput(BaseModel):
    """Input for tool A."""

    model_config = ConfigDict(extra="forbid")

    param_x: float = Field(description="First parameter")
    param_y: float = Field(description="Second parameter")


class ToolAResult(BaseModel):
    """Result of tool A."""

    value: float = Field(description="Computed result value")


@pytest.fixture
def mcp_a() -> EnhancedFastMCP:
    """Create MCP server A with a simple flat tool."""
    mcp = EnhancedFastMCP()

    @mcp.flat_model()
    def tool_a(input: ToolAInput) -> ToolAResult:
        """Perform tool A on the inputs."""
        return ToolAResult(value=input.param_x + input.param_y)

    return mcp


class NestedInfo(BaseModel):
    """Nested information block."""

    model_config = ConfigDict(extra="forbid")

    regex: Annotated[str, Field(description="Regex validation", pattern=r"^\d{5}$")]
    text_defaultd: str = Field(default="DEFAULT", description="Text with default")


class CategoryInfo(BaseModel):
    """Category classification."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["type_a", "type_b", "type_c"] = Field(default="type_b")


class ToolBInput(BaseModel):
    """Complete tool B request.

    Exercises nested models, annotated fields with regex validation, field-level
    descriptions, model-level documentation.
    """

    model_config = ConfigDict(extra="forbid")

    identifier: Annotated[str, Field(description="Required regex field", pattern=r"^[a-z]{3}$")]
    count: int = Field(description="Int with range", ge=10, le=100)
    nested: NestedInfo
    category: CategoryInfo = Field(default_factory=lambda: CategoryInfo(type="type_b"))
    flag: bool = Field(default=False, description="Boolean with default")


class ResponseA(BaseModel):
    """Success response variant A."""

    status: Literal["success"] = "success"
    message: str = Field(description="Status message")


class ResponseB(BaseModel):
    """Error response variant B."""

    status: Literal["error"] = "error"
    error_code: str = Field(description="Machine-readable error code")
    message: str = Field(description="Human-readable error description")


ToolBResult = Annotated[ResponseA | ResponseB, Field(discriminator="status")]


@pytest.fixture
def mcp_b() -> EnhancedFastMCP:
    """Create MCP server B with complex nested schema."""
    mcp = EnhancedFastMCP()

    @mcp.flat_model()
    def tool_b(input: ToolBInput) -> ToolBResult:
        """Perform tool B with complex validation."""
        return ResponseB(error_code="TEST", message="test")

    return mcp


async def test_agent_compositor_flat_tools_request_schema(compositor, compositor_client, mcp_a, mcp_b) -> None:
    """Test agent with 2 flat MCP servers attached one by one, showing schema evolution.

    This test demonstrates:
    1. Mounting mcp_a alone -> schema has only tool_a
    2. Mounting both servers -> schema has both tool_a and tool_b
    3. Complex Pydantic schema with:
       - Annotated fields with regex patterns
       - Field descriptions
       - Model-level descriptions
       - Nested models (NestedInfo, CategoryInfo)
    4. Full request structure with all tools/schemas visible to the LLM
    """
    # Mount only mcp_a for phase 1 and capture Mounted object
    mounted_a = await compositor.mount_inproc(MCPMountPrefix("mcp_a"), mcp_a)

    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        # Phase 1: only mcp_a mounted
        req = yield
        assert req.tools is not None
        assert {"mcp_a_tool_a"} <= {t.name for t in req.tools}
        print("\nPHASE 1 REQUEST (mcp_a only):")
        print(json.dumps(req.model_dump(exclude_none=True), indent=2))

        yield m.mcp_tool_call(mounted_a.prefix, TOOL_A_NAME, ToolAInput(param_x=10, param_y=20))
        yield m.assistant_text("The result is 30.")

        # Phase 2: both servers mounted
        req = yield
        assert req.tools is not None
        assert {"mcp_a_tool_a", "mcp_b_tool_b"} <= {t.name for t in req.tools}
        print("\nPHASE 2 REQUEST (mcp_a + mcp_b):")
        print(json.dumps(req.model_dump(exclude_none=True), indent=2))

        # Detailed schema assertions
        tool_a = next(t for t in req.tools if t.name == "mcp_a_tool_a")
        assert tool_a.description == "Perform tool A on the inputs."
        assert tool_a.type == "function"
        assert tool_a.parameters["type"] == "object"
        assert tool_a.parameters["properties"]["param_x"]["type"] == "number"
        assert tool_a.parameters["properties"]["param_y"]["type"] == "number"
        assert set(tool_a.parameters["required"]) == {"param_x", "param_y"}

        tool_b = next(t for t in req.tools if t.name == "mcp_b_tool_b")
        assert "Perform tool B with complex validation" in tool_b.description
        assert tool_b.type == "function"
        params_b = tool_b.parameters
        assert params_b["type"] == "object"

        # Top-level fields
        props = params_b["properties"]
        assert props["identifier"]["type"] == "string"
        assert props["count"]["type"] == "integer"
        assert "$ref" in props["nested"]
        assert "$ref" in props["category"]
        assert props["flag"]["type"] == "boolean"
        assert set(params_b["required"]) == {"identifier", "count", "nested", "category", "flag"}

        # Nested models in $defs
        assert "$defs" in params_b
        nested_def = params_b["$defs"]["NestedInfo"]
        assert nested_def["properties"]["regex"]["pattern"] == r"^\d{5}$"
        assert nested_def["properties"]["text_defaultd"]["default"] == "DEFAULT"

        category_def = params_b["$defs"]["CategoryInfo"]
        assert set(category_def["properties"]["type"]["enum"]) == {"type_a", "type_b", "type_c"}

        print("\nMCP_A_TOOL_A SCHEMA:")
        print(json.dumps(tool_a.model_dump(exclude_none=True), indent=2))

        print("\nMCP_B_TOOL_B SCHEMA:")
        print(json.dumps(tool_b.model_dump(exclude_none=True), indent=2))

        yield m.mcp_tool_call(mounted_a.prefix, TOOL_A_NAME, ToolAInput(param_x=10, param_y=20))
        yield m.assistant_text("The result is 30.")

    system_prompt = "You are a helpful assistant. Calculate 10 + 20."

    print("PHASE 1: MCP_A ONLY")

    agent = await Agent.create(
        mcp_client=compositor_client,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        parallel_tool_calls=False,
        tool_policy=RequireAnyTool(),
    )
    agent.process_message(SystemMessage.text(system_prompt))
    await agent.run()

    print("PHASE 2: MCP_A + MCP_B")

    # Mount mcp_b for phase 2 (mcp_a already mounted)
    await compositor.mount_inproc(MCPMountPrefix("mcp_b"), mcp_b)

    agent = await Agent.create(
        mcp_client=compositor_client,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        parallel_tool_calls=False,
        tool_policy=RequireAnyTool(),
    )
    agent.process_message(SystemMessage.text(system_prompt))
    await agent.run()

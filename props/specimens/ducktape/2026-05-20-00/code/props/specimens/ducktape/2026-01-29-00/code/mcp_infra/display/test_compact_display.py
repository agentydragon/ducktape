"""Snapshot tests for CompactDisplayHandler."""

from __future__ import annotations

import json
from io import StringIO
from typing import cast

import pytest
import pytest_bazel
from mcp.types import Implementation, InitializeResult, ReadResourceResult, ServerCapabilities, TextResourceContents
from pydantic import AnyUrl, BaseModel
from rich.console import Console
from syrupy.assertion import SnapshotAssertion

from agent_core.events import ToolCall, ToolCallOutput
from agent_core.tool_provider import ToolResult
from mcp_infra.display.rich_display import CompactDisplayHandler
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.models import BaseExecResult, ExecInput, Exited
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix


def render_handler_to_string(call: ToolCall, output: ToolCallOutput, prefix: str = "Agent") -> str:
    """Helper to render CompactDisplayHandler events to string.

    Simulates the sequence: tool call -> tool output.
    """
    out = StringIO()
    console = Console(file=out, width=80, legacy_windows=False, color_system=None)

    # Register tool schemas so the handler can recognize ExecInput/BaseExecResult
    # Use tuple keys (MCPMountPrefix, tool_name) as expected by CompactDisplayHandler
    tool_input_schemas: dict[tuple[MCPMountPrefix, str], type[BaseModel]] = {
        (ContainerExecServer.RUNTIME_MOUNT_PREFIX, ContainerExecServer.EXEC_TOOL_NAME): cast(type[BaseModel], ExecInput)
    }
    tool_schemas: dict[tuple[MCPMountPrefix, str], type[BaseModel]] = {
        (ContainerExecServer.RUNTIME_MOUNT_PREFIX, ContainerExecServer.EXEC_TOOL_NAME): cast(
            type[BaseModel], BaseExecResult
        )
    }

    # Create handler with the console and schemas
    test_handler = CompactDisplayHandler(max_lines=20, console=console, prefix=prefix, show_token_usage=False)
    test_handler._tool_input_schemas = tool_input_schemas
    test_handler._tool_schemas = tool_schemas

    # Feed events in order
    test_handler.on_tool_call_event(call)
    test_handler.on_tool_result_event(output)

    return out.getvalue()


@pytest.mark.parametrize(
    "cmd",
    [
        pytest.param(["bash", "-c", "ls -la /workspace"], id="bash_-c"),
        pytest.param(["sh", "-c", "echo hello | tr a-z A-Z"], id="sh_-c"),
        pytest.param(["/bin/sh", "-lc", "sed -n '1,10p' file.txt"], id="/bin/sh_-lc"),
        pytest.param(["ruff", "check", "/workspace"], id="non_wrapped"),
        pytest.param(["python", "-c", "print('hello world')"], id="non_wrapped_spaces"),
    ],
)
def test_docker_exec_shell_unwrapping_snapshot(snapshot: SnapshotAssertion, call_id_gen, cmd: list[str]):
    """Snapshot test for docker exec command display with shell unwrapping.

    Tests the _unwrap_shell_command() logic for various shell wrappers.
    """
    # Create ExecInput
    exec_input = ExecInput(cmd=cmd, cwd="/workspace", env=None, user=None, timeout_ms=30000)

    # Create ToolCall with ExecInput
    call = ToolCall(
        name=build_mcp_function(ContainerExecServer.RUNTIME_MOUNT_PREFIX, ContainerExecServer.EXEC_TOOL_NAME),
        args_json=json.dumps(exec_input.model_dump()),
        call_id=call_id_gen(),
    )

    # Create BaseExecResult (successful exit)
    exec_result = BaseExecResult(exit=Exited(exit_code=0), stdout="output text\n", stderr="", duration_ms=125)

    # Create ToolCallOutput with BaseExecResult
    output = ToolCallOutput(
        call_id=call.call_id, result=ToolResult(content=[], structured_content=exec_result.model_dump(), is_error=False)
    )

    # Render to string
    rendered = render_handler_to_string(call, output)

    # Compare against snapshot
    assert rendered == snapshot


def test_docker_exec_with_custom_cwd_snapshot(snapshot: SnapshotAssertion, call_id_gen):
    """Snapshot test for docker exec with custom working directory display."""
    # Create ExecInput with custom cwd
    exec_input = ExecInput(cmd=["bash", "-c", "pwd && ls"], cwd="/tmp/custom", env=None, user=None, timeout_ms=30000)

    # Create ToolCall
    call = ToolCall(
        name=build_mcp_function(ContainerExecServer.RUNTIME_MOUNT_PREFIX, ContainerExecServer.EXEC_TOOL_NAME),
        args_json=json.dumps(exec_input.model_dump()),
        call_id=call_id_gen(),
    )

    # Create BaseExecResult
    exec_result = BaseExecResult(
        exit=Exited(exit_code=0), stdout="/tmp/custom\nfile1.txt\nfile2.py\n", stderr="", duration_ms=89
    )

    # Create ToolCallOutput
    output = ToolCallOutput(
        call_id=call.call_id, result=ToolResult(content=[], structured_content=exec_result.model_dump(), is_error=False)
    )

    # Render to string
    rendered = render_handler_to_string(call, output)

    # Compare against snapshot
    assert rendered == snapshot


# CompactDisplayHandler serialization tests (Pydantic Url/AnyUrl handling)


def test_compact_display_handler_with_anyurl_in_result():
    """Test that CompactDisplayHandler handles tool results with AnyUrl correctly.

    This reproduces the original bug: InitializeResult contains serverInfo.url (AnyUrl),
    and without mode='json', serialization would fail.
    """

    # Create a realistic MCP InitializeResult - this is what would come back from an MCP server
    # The serverInfo.url field is Optional[AnyUrl], which caused the original bug
    init_result = InitializeResult(
        protocolVersion="2024-11-05",
        capabilities=ServerCapabilities(),
        serverInfo=Implementation(name="test-server", version="1.0.0"),
    )

    # The bug was: when we try to dump this without mode="json", it contains Url objects
    # that can't be serialized by json.dumps() or compact_json Formatter
    dumped_without_mode = init_result.model_dump()  # This would have Url objects

    # Now create a ToolResult with this as structured content (simulating a tool returning it)
    result = ToolResult(structured_content=dumped_without_mode, is_error=False, content=[])

    # Create handler with a StringIO console to capture output
    output_buffer = StringIO()
    console = Console(file=output_buffer, force_terminal=False, width=120)
    handler = CompactDisplayHandler(max_lines=20, console=console, servers=None, show_token_usage=False)

    # Create a fake tool call and output event

    call = ToolCall(call_id="test-123", name="mcp_initialize", args_json="{}")
    handler._calls["test-123"] = call

    output = ToolCallOutput(call_id="test-123", result=result)

    # This used to raise "TypeError: Object of type Url is not JSON serializable"
    # Now it should work because the handler uses to_jsonable_python()
    handler.on_tool_result_event(output)
    output_text = output_buffer.getvalue()

    # Should have produced some output
    assert len(output_text) > 0, "Handler should have produced output"
    # Should contain the server name
    assert "test-server" in output_text, f"Output should contain server name, got: {output_text}"


def test_compact_display_handler_with_read_resource_result():
    """Test that CompactDisplayHandler handles ReadResourceResult correctly.

    ReadResourceResult was in the original error trace - it contains uri (AnyUrl).
    """

    # Create a ReadResourceResult - this is what resources_read_blocks returns
    read_result = ReadResourceResult(
        contents=[
            TextResourceContents(
                uri=AnyUrl("resource://docker/containers/snapshots/crush/2025-08-30-internal_db/info"),
                mimeType="text/plain",
                text="test content",
            )
        ]
    )

    # Dump without mode - this will have Url objects
    dumped = read_result.model_dump()

    # Create a ToolResult with this as structured content
    result = ToolResult(structured_content=dumped, is_error=False, content=[])

    # Create handler with StringIO console
    output_buffer = StringIO()
    console = Console(file=output_buffer, force_terminal=False, width=120)
    handler = CompactDisplayHandler(max_lines=20, console=console, servers=None, show_token_usage=False)

    call = ToolCall(call_id="test-456", name="resources_read_blocks", args_json='{"uri": "test"}')
    handler._calls["test-456"] = call

    output = ToolCallOutput(call_id="test-456", result=result)

    # This used to raise "TypeError: Object of type Url is not JSON serializable"
    handler.on_tool_result_event(output)
    output_text = output_buffer.getvalue()
    assert len(output_text) > 0, "Handler should have produced output"
    # Should contain the resource URI or content
    assert "resource://" in output_text or "test content" in output_text, (
        f"Output missing expected content: {output_text}"
    )


if __name__ == "__main__":
    pytest_bazel.main()

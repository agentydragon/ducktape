"""Snapshot tests for CompactDisplayHandler."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import cast

import pytest
import pytest_bazel
from mcp.types import ListRootsResult, Root
from pydantic import BaseModel, FileUrl
from rich.console import Console
from syrupy.assertion import SnapshotAssertion

from agent_core.events import ToolCall, ToolCallOutput
from agent_core.tool_provider import ToolResult
from mcp_infra.display.rich_display import CompactDisplayHandler
from mcp_infra.exec.docker.server import ContainerExecServer, _make_exec_input_model
from mcp_infra.exec.docker.types import DefaultValue
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from util.exec.models import BaseExecResult, Exited

# Dynamic model matching a server with all fields enabled
_TestExecInput: type[BaseModel] = _make_exec_input_model(
    allow_user=True, allow_env=True, cwd_policy=DefaultValue(value=Path("/test/workspace"))
)


def render_handler_to_string(call: ToolCall, output: ToolCallOutput, prefix: str = "Agent") -> str:
    """Helper to render CompactDisplayHandler events to string.

    Simulates the sequence: tool call -> tool output.
    """
    out = StringIO()
    console = Console(file=out, width=80, legacy_windows=False, color_system=None)

    # Register tool schemas so the handler can recognize _TestExecInput/BaseExecResult
    # Use tuple keys (MCPMountPrefix, tool_name) as expected by CompactDisplayHandler
    tool_input_schemas: dict[tuple[MCPMountPrefix, str], type[BaseModel]] = {
        (ContainerExecServer.RUNTIME_MOUNT_PREFIX, ContainerExecServer.EXEC_TOOL_NAME): cast(
            type[BaseModel], _TestExecInput
        )
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
        pytest.param(["bash", "-c", "ls -la /test/workspace"], id="bash_-c"),
        pytest.param(["ruff", "check", "/test/workspace"], id="non_wrapped"),
        pytest.param(["python", "-c", "print('hello world')"], id="non_wrapped_spaces"),
    ],
)
def test_docker_exec_shell_unwrapping_snapshot(snapshot: SnapshotAssertion, call_id_gen, cmd: list[str]):
    """Snapshot test for docker exec command display with shell unwrapping.

    Tests the _unwrap_shell_command() logic for various shell wrappers.
    """
    exec_input = _TestExecInput(cmd=cmd, cwd="/test/workspace", env=None, user=None, timeout_ms=30000)

    call = ToolCall(
        name=build_mcp_function(ContainerExecServer.RUNTIME_MOUNT_PREFIX, ContainerExecServer.EXEC_TOOL_NAME),
        args_json=json.dumps(exec_input.model_dump()),
        call_id=call_id_gen(),
    )

    exec_result = BaseExecResult(
        exit=Exited(exit_code=0), stdout="output text\nsecond line\n", stderr="", duration_ms=125
    )

    output = ToolCallOutput(
        call_id=call.call_id, result=ToolResult(content=[], structured_content=exec_result.model_dump(), is_error=False)
    )

    rendered = render_handler_to_string(call, output)

    assert rendered == snapshot


def test_compact_display_renders_pydantic_url_in_result(call_id_gen):
    """A python-mode `model_dump()` leaves pydantic `Url` objects (`Root.uri` is a `FileUrl`) that `json.dumps` rejects."""
    call = ToolCall(
        name=build_mcp_function(ContainerExecServer.RUNTIME_MOUNT_PREFIX, "list_roots"),
        args_json="{}",
        call_id=call_id_gen(),
    )
    roots = ListRootsResult(roots=[Root(uri=FileUrl("file:///test/workspace"), name="workspace")])
    output = ToolCallOutput(call_id=call.call_id, result=ToolResult(structured_content=roots.model_dump()))

    assert "file:///test/workspace" in render_handler_to_string(call, output)


if __name__ == "__main__":
    pytest_bazel.main()

"""Bootstrap validation utilities for agent tests.

These are adgn-specific utilities for validating that bootstrap docker exec
commands completed successfully before running test scenarios.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from agent_core.agent import _openai_to_mcp_result
from agent_core_testing.responses import ResponsesFactory
from agent_core_testing.steps import DockerExecCall, MakeCall
from mcp_infra.calltool import extract_structured_content
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.models import BaseExecResult, Exited, Killed, TimedOut, TruncatedStream
from mcp_infra.naming import parse_tool_name
from openai_utils.model import FunctionCallItem, FunctionCallOutputItem, ResponsesRequest, ResponsesResult

logger = logging.getLogger(__name__)

# Directory for bootstrap output dumps
BOOTSTRAP_DUMPS_DIR = Path(__file__).parent.parent.parent.parent.parent / "tests" / "props" / "bootstrap_dumps"


def _is_runtime_exec_tool(tool_name: str) -> bool:
    """Check if tool_name is a runtime exec tool."""
    try:
        prefix, tool = parse_tool_name(tool_name)
        return prefix == ContainerExecServer.DOCKER_MOUNT_PREFIX and tool == "exec"
    except ValueError:
        return False


def _get_stream_size(stream: str | TruncatedStream) -> int:
    """Get the byte size of a stdout/stderr stream."""
    if isinstance(stream, TruncatedStream):
        return stream.total_bytes
    return len(stream.encode("utf-8"))


def _get_stream_text(stream: str | TruncatedStream) -> str:
    """Get the text content of a stdout/stderr stream."""
    if isinstance(stream, TruncatedStream):
        return stream.truncated_text
    return stream


def _dump_bootstrap_output(exec_result: BaseExecResult, cmd_args: str, test_name: str | None = None) -> Path:
    """Dump bootstrap init output to a file for inspection."""
    BOOTSTRAP_DUMPS_DIR.mkdir(parents=True, exist_ok=True)

    name = test_name or "unknown"
    stdout_path = BOOTSTRAP_DUMPS_DIR / f"{name}.stdout"
    stderr_path = BOOTSTRAP_DUMPS_DIR / f"{name}.stderr"

    stdout_path.write_text(_get_stream_text(exec_result.stdout))
    stderr_path.write_text(_get_stream_text(exec_result.stderr))

    logger.info("Bootstrap output dumped to: %s, %s", stdout_path, stderr_path)
    return stdout_path


def assert_bootstrap_exec_success(req: ResponsesRequest, *, test_name: str | None = None) -> None:
    """Assert all runtime exec calls in the request completed with exit code 0."""
    if test_name is None:
        raw = os.environ.get("PYTEST_CURRENT_TEST", "")
        test_name = raw.split("::")[-1].split("[")[0].split(" ")[0]

    if isinstance(req.input, str):
        return

    call_id_to_tool: dict[str, str] = {}
    call_id_to_args: dict[str, str] = {}
    for item in req.input:
        if isinstance(item, FunctionCallItem):
            call_id_to_tool[item.call_id] = item.name
            args_str = item.arguments if isinstance(item.arguments, str) else json.dumps(item.arguments)
            call_id_to_args[item.call_id] = args_str

    failures: list[str] = []
    bootstrap_sizes: list[tuple[str, int, int]] = []

    for item in req.input:
        if not isinstance(item, FunctionCallOutputItem):
            continue

        call_id = item.call_id
        tool_name = call_id_to_tool.get(call_id)

        if tool_name is None or not _is_runtime_exec_tool(tool_name):
            continue

        if item.output is None:
            failures.append(f"Runtime exec call has no output (tool={tool_name}, call_id={call_id})")
            continue

        try:
            result = _openai_to_mcp_result(item.output)
            exec_result = extract_structured_content(result, BaseExecResult)
        except (ValueError, TypeError) as e:
            output_preview = str(item.output)[:500]
            failures.append(
                f"Failed to parse runtime exec result (tool={tool_name}, call_id={call_id}):\n"
                f"  parse error: {e}\n"
                f"  raw output: {output_preview!r}"
            )
            continue

        stdout_bytes = _get_stream_size(exec_result.stdout)
        stderr_bytes = _get_stream_size(exec_result.stderr)
        cmd_args = call_id_to_args.get(call_id, "unknown")
        bootstrap_sizes.append((cmd_args[:50], stdout_bytes, stderr_bytes))

        _dump_bootstrap_output(exec_result, cmd_args, test_name=test_name)

        if isinstance(exec_result.exit, TimedOut):
            failures.append(
                f"Bootstrap command TIMED OUT (tool={tool_name}, call_id={call_id}):\n"
                f"  {exec_result.model_dump_json(indent=2)}"
            )
        elif isinstance(exec_result.exit, Exited):
            if exec_result.exit.exit_code != 0:
                failures.append(
                    f"Bootstrap command FAILED with exit_code={exec_result.exit.exit_code} "
                    f"(tool={tool_name}, call_id={call_id}):\n"
                    f"  {exec_result.model_dump_json(indent=2)}"
                )
        elif isinstance(exec_result.exit, Killed):
            failures.append(
                f"Bootstrap command was KILLED (tool={tool_name}, call_id={call_id}):\n"
                f"  {exec_result.model_dump_json(indent=2)}"
            )

    if bootstrap_sizes:
        total_stdout = sum(s[1] for s in bootstrap_sizes)
        total_stderr = sum(s[2] for s in bootstrap_sizes)
        logger.info(
            "Bootstrap output sizes: %d commands, %d bytes stdout, %d bytes stderr (total: %d bytes)",
            len(bootstrap_sizes),
            total_stdout,
            total_stderr,
            total_stdout + total_stderr,
        )
        for cmd_preview, stdout_bytes, stderr_bytes in bootstrap_sizes:
            logger.debug("  %s: stdout=%d, stderr=%d", cmd_preview, stdout_bytes, stderr_bytes)

    if failures:
        raise AssertionError(
            f"Bootstrap runtime exec commands failed ({len(failures)} failures):\n\n" + "\n\n".join(failures)
        )


@dataclass
class AssertBootstrapSuccess:
    """Assert all bootstrap docker exec commands succeeded."""

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        assert_bootstrap_exec_success(req)
        return factory.make_assistant_message("Bootstrap validated successfully")


@dataclass
class MakeCallWithBootstrapValidation(MakeCall):
    """Validate bootstrap succeeded, then make a tool call."""

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        assert_bootstrap_exec_success(req)
        return super().execute(req, factory)


@dataclass
class DockerExecCallWithBootstrapValidation(DockerExecCall):
    """Make a docker exec call after validating bootstrap succeeded."""

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        assert_bootstrap_exec_success(req)
        return super().execute(req, factory)

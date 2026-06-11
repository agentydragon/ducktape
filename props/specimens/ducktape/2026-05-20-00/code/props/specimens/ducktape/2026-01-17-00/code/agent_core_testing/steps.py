"""Declarative step classes for test agent state machines.

Provides portable step types for building mock agent conversations:
- Step: Protocol for all step objects
- AssistantMessage: Return assistant message
- EchoCall: Call echo test server
- MakeCall: Make a tool call with server prefix
- DockerExecCall: Make a docker exec tool call
- AssertDockerExecThenFinish/Call: Assert docker exec succeeded
- Finish: Assert completion and return message
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from hamcrest import all_of, assert_that
from hamcrest.core.base_matcher import BaseMatcher
from hamcrest.core.description import Description
from hamcrest.core.matcher import Matcher
from pydantic import BaseModel, ConfigDict

from agent_core_testing.assertions import assert_and_extract, assert_last_call
from agent_core_testing.echo_server import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME, EchoInput
from mcp_infra.exec.models import BaseExecResult, Exited, TruncatedStream
from mcp_infra.prefix import MCPMountPrefix

if TYPE_CHECKING:
    from agent_core_testing.responses import ResponsesFactory
    from openai_utils.model import ResponsesRequest, ResponsesResult

logger = logging.getLogger(__name__)


def _assert_docker_exec_success(
    output: BaseExecResult, stdout_matchers: list[Matcher[str]], expected_output: str
) -> None:
    """Assert docker exec succeeded with exit code 0 and stdout matches expectations."""
    if not isinstance(output.exit, Exited) or output.exit.exit_code != 0:
        stderr_text = output.stderr if isinstance(output.stderr, str) else output.stderr.truncated_text
        stdout_text = output.stdout if isinstance(output.stdout, str) else output.stdout.truncated_text
        raise AssertionError(f"Expected exit code 0, got {output.exit}\nstdout: {stdout_text}\nstderr: {stderr_text}")

    stdout_text = output.stdout if isinstance(output.stdout, str) else output.stdout.truncated_text
    logger.info(
        "docker_exec succeeded: exit=%s stdout_bytes=%d stderr_bytes=%d sample=%r",
        output.exit,
        len(stdout_text.encode("utf-8")) if stdout_text else 0,
        len(output.stderr.encode("utf-8")) if isinstance(output.stderr, str) else 0,
        (stdout_text or "")[:120],
    )

    if stdout_matchers:
        assert_that(stdout_text, all_of(*stdout_matchers))
    elif expected_output and expected_output not in stdout_text:
        raise AssertionError(f"Expected stdout to contain {expected_output!r}, got {stdout_text!r}")


def _get_stream_text(stream: str | TruncatedStream) -> str:
    """Get the text content of a stdout/stderr stream."""
    if isinstance(stream, TruncatedStream):
        return stream.truncated_text
    return stream


class EmptyArgs(BaseModel):
    """Empty arguments for zero-parameter MCP tools."""

    model_config = ConfigDict(extra="forbid")


class Step(Protocol):
    """Protocol for step objects that can be executed in sequence."""

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult: ...


@dataclass
class AssistantMessage:
    """Return assistant message without checking previous tool."""

    message: str

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        return factory.make_assistant_message(self.message)


@dataclass
class EchoCall:
    """Call echo test server."""

    text: str

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        tool_name = f"{ECHO_MOUNT_PREFIX}_{ECHO_TOOL_NAME}"
        return factory.make_tool_call(tool_name, EchoInput(text=self.text).model_dump())


@dataclass
class MakeCall:
    """Make a tool call with server prefix, tool name, and typed arguments."""

    server: MCPMountPrefix
    tool: str
    args: BaseModel

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        return factory.make_mcp_tool_call(self.server, self.tool, self.args)


@dataclass
class CheckThenCall:
    """Assert previous tool completed, then call next."""

    expected_tool: str
    server: MCPMountPrefix
    tool: str
    args: BaseModel

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        assert_last_call(req, self.expected_tool)
        return factory.make_mcp_tool_call(self.server, self.tool, self.args)


@dataclass
class ExtractThenCall[T: BaseModel]:
    """Extract typed output from previous call, use in next call."""

    expected_tool: str
    output_type: type[T]
    make_next: Callable[[T], tuple[MCPMountPrefix, str, BaseModel]]

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        output = assert_and_extract(req, self.expected_tool, self.output_type)
        server, tool, args = self.make_next(output)
        return factory.make_mcp_tool_call(server, tool, args)


@dataclass
class Finish:
    """Final turn: assert completion and return message."""

    expected_tool: str
    message: str = "Done"

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        assert_last_call(req, self.expected_tool)
        return factory.make_assistant_message(self.message)


@dataclass
class DockerExecCall:
    """Make a docker exec tool call with convenient parameters."""

    cmd: list[str]
    timeout_ms: int = 30000
    cwd: Path | None = None
    env: list[str] | None = None
    user: str | None = None
    tool_name: str = "exec"

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        return factory.make(
            factory.docker_exec(
                self.cmd,
                timeout_ms=self.timeout_ms,
                cwd=self.cwd,
                env=self.env,
                user=self.user,
                tool_name=self.tool_name,
            )
        )


@dataclass
class AssertDockerExecThenFinish:
    """Assert docker exec succeeded and stdout matches expectations, then finish."""

    expected_output: str
    message: str = "Done"
    stdout_matchers: list[Matcher[str]] = field(default_factory=list)

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        assert_last_call(req, "docker_exec")
        output = assert_and_extract(req, "docker_exec", BaseExecResult)
        _assert_docker_exec_success(output, self.stdout_matchers, self.expected_output)
        return factory.make_assistant_message(self.message)


@dataclass
class AssertDockerExecThenCall:
    """Assert docker exec succeeded and stdout matches expectations, then make another call."""

    expected_output: str
    next_cmd: list[str]
    timeout_ms: int = 30000
    tool_name: str = "exec"
    stdout_matchers: list[Matcher[str]] = field(default_factory=list)

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        assert_last_call(req, "docker_exec")
        output = assert_and_extract(req, "docker_exec", BaseExecResult)
        _assert_docker_exec_success(output, self.stdout_matchers, self.expected_output)
        return factory.make(factory.docker_exec(self.next_cmd, timeout_ms=self.timeout_ms, tool_name=self.tool_name))


# =============================================================================
# Hamcrest Matchers for BaseExecResult
# =============================================================================


class ExitedSuccessfully(BaseMatcher[BaseExecResult]):
    """Matcher for BaseExecResult that exited with code 0."""

    def _matches(self, item: BaseExecResult) -> bool:
        return isinstance(item.exit, Exited) and item.exit.exit_code == 0

    def describe_to(self, description: Description) -> None:
        description.append_text("BaseExecResult with exit code 0")

    def describe_mismatch(self, item: BaseExecResult, mismatch_description: Description) -> None:
        stderr = _get_stream_text(item.stderr)
        stdout = _get_stream_text(item.stdout)
        mismatch_description.append_text(f"exit was {item.exit}\nstdout: {stdout}\nstderr: {stderr}")


def exited_successfully() -> Matcher[BaseExecResult]:
    """Matcher for BaseExecResult that exited with code 0."""
    return ExitedSuccessfully()


class StdoutContains(BaseMatcher[BaseExecResult]):
    """Matcher for BaseExecResult whose stdout contains expected text."""

    def __init__(self, expected: str) -> None:
        self._expected = expected

    def _matches(self, item: BaseExecResult) -> bool:
        stdout = _get_stream_text(item.stdout)
        return self._expected in stdout

    def describe_to(self, description: Description) -> None:
        description.append_text(f"stdout containing {self._expected!r}")

    def describe_mismatch(self, item: BaseExecResult, mismatch_description: Description) -> None:
        stdout = _get_stream_text(item.stdout)
        mismatch_description.append_text(f"stdout was {stdout!r}")


def stdout_contains(expected: str) -> Matcher[BaseExecResult]:
    """Matcher for BaseExecResult whose stdout contains expected text."""
    return StdoutContains(expected)

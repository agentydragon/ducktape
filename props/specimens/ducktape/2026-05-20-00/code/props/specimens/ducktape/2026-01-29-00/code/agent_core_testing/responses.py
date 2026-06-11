"""Response factories and mock runners for agent tests.

Provides declarative test response building:
- ResponsesFactory: Builds mock ResponsesResult objects
- GeneratorMock + DockerExecMock: Class-based generator mocks with yield from
- PendingCall: Typed tool call wrapper for roundtrip patterns
- extract_call_output: Extract typed tool outputs from requests
"""

from __future__ import annotations

import json
import logging
import os
from abc import abstractmethod
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.tools.tool import Tool
from pydantic import BaseModel, TypeAdapter

from agent_core_testing.echo_server import ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME, EchoInput, EchoOutput
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.models import BaseExecResult, ExecInput, make_exec_input
from mcp_infra.mounted import Mounted
from mcp_infra.naming import MCPMountPrefix, build_mcp_function
from openai_utils.builders import ItemFactory
from openai_utils.model import (
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    InputTokensDetails,
    OpenAIModelProto,
    OutputTokensDetails,
    ResponseOutItem,
    ResponsesRequest,
    ResponsesResult,
    ResponseUsage,
)

logger = logging.getLogger(__name__)


class ResponsesFactory(ItemFactory):
    """Convenience adapter response builders bound to a model name.

    Provides methods to build mock ResponsesResult objects for testing.
    """

    def __init__(self, model: str):
        super().__init__(call_id_prefix="test")
        self.model = model

    def make_assistant_message(self, text: str) -> ResponsesResult:
        return ResponsesResult(
            id="resp_msg",
            usage=ResponseUsage(
                input_tokens=0,
                input_tokens_details=InputTokensDetails(cached_tokens=0),
                output_tokens=1,
                output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
                total_tokens=1,
            ),
            output=[self.assistant_text(text)],
        )

    def make_tool_call(self, name: str, arguments: dict[str, Any], call_id: str | None = None) -> ResponsesResult:
        return self.make(self.tool_call(name, arguments, call_id))

    def make_mcp_tool_call(self, server: MCPMountPrefix, tool: str, arguments: BaseModel) -> ResponsesResult:
        """Create tool call response for MCP server/tool with automatic naming."""
        return self.make(self.mcp_tool_call(server, tool, arguments))

    # ---- Low-level item builders (compose with make(...items)) ----

    def mcp_tool_call(
        self, server: MCPMountPrefix, tool: str, arguments: BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create tool call for MCP server/tool with automatic naming."""
        return self.tool_call(build_mcp_function(server, tool), arguments.model_dump(mode="json"), call_id)

    # ---- Message/response constructors (compose items) ----

    def make(self, *items: ResponseOutItem) -> ResponsesResult:
        out_tokens = sum(max(1, len(it.text)) for it in items if isinstance(it, AssistantMessageOut))
        return ResponsesResult(
            id="resp_generic",
            usage=ResponseUsage(
                input_tokens=0,
                input_tokens_details=InputTokensDetails(cached_tokens=0),
                output_tokens=(1 if out_tokens else 0),
                output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
                total_tokens=(1 if out_tokens else 0),
            ),
            output=list(items),
        )

    def make_final_assistant(self, text: str) -> ResponsesResult:
        return self.make(self.assistant_text(text))

    def docker_exec(
        self,
        cmd: list[str],
        *,
        timeout_ms: int = 30000,
        cwd: Path | None = None,
        env: list[str] | None = None,
        user: str | None = None,
        tool_name: str = "exec",
    ) -> FunctionCallItem:
        """Create docker exec tool call with sensible defaults."""
        exec_input = ExecInput(cmd=cmd, cwd=str(cwd) if cwd else None, env=env, user=user, timeout_ms=timeout_ms)
        return self.mcp_tool_call(ContainerExecServer.DOCKER_MOUNT_PREFIX, tool_name, exec_input)

    def mounted_tool_call[S: FastMCP](
        self, mounted: Mounted[S], tool: Tool, arguments: BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create tool call from Mounted server + tool attribute.

        Preferred over mcp_tool_call when you have a Mounted wrapper, as it
        derives the fully-qualified tool name from the Tool attribute.
        """
        return self.tool_call(mounted.tool_name(tool), arguments.model_dump(mode="json"), call_id)


# Type for generator that yields responses and receives requests
MockScriptGen = Generator[ResponsesResult | ResponseOutItem | list[ResponseOutItem] | None, ResponsesRequest]


class GeneratorRunner(OpenAIModelProto):
    """Generator-based OpenAI mock with runtime 1:1 request-response enforcement.

    Use via the @openai_mock decorator for concise test code:

        @openai_mock
        def mock(factory):
            req = yield  # Receive first request
            call = factory.docker_exec(["ls"])
            req = yield call  # Send response, receive next request
            yield factory.assistant_text("Done")

        agent = await Agent.create(client=mock, ...)
    """

    def __init__(self, gen: MockScriptGen, factory: ResponsesFactory) -> None:
        self._factory = factory
        self._gen = gen
        # Prime: advance to first yield. next() is equivalent to send(None)
        # but avoids type issues with Generator's send signature
        next(self._gen)
        self.model = "test-model"

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        """Send request to generator, return wrapped response."""
        try:
            result = self._gen.send(req)
        except StopIteration:
            raise RuntimeError("Mock exhausted: generator ended but received another request") from None

        return self._wrap(result)

    def _wrap(self, result: ResponsesResult | ResponseOutItem | list[ResponseOutItem] | None) -> ResponsesResult:
        """Auto-wrap yielded values to ResponsesResult."""
        if result is None:
            raise RuntimeError("Generator yielded None when response expected")
        if isinstance(result, ResponsesResult):
            return result
        if isinstance(result, list):
            return self._factory.make(*result)
        return self._factory.make(result)


# Type alias for generator function that takes factory and returns a mock generator
MockScriptFn = Callable[[ResponsesFactory], MockScriptGen]


def openai_mock(fn: MockScriptFn) -> GeneratorRunner:
    """Convert a generator function into an OpenAIModelProto mock.

    The generator function should:
    1. Accept a ResponsesFactory as argument
    2. Start with `req = yield` to receive the first request
    3. Yield responses (ResponsesResult, single item, or list of items)
    4. Receive next request via `req = yield response`

    Example:
        @openai_mock
        def mock(factory):
            req = yield  # First request
            call = factory.docker_exec(["ls"])
            req = yield call
            yield factory.assistant_text("Done")
    """
    factory = ResponsesFactory("test-model")
    gen = fn(factory)
    return GeneratorRunner(gen, factory)


def extract_call_output[T: BaseModel](req: ResponsesRequest, call: FunctionCallItem, output_type: type[T]) -> T:
    """Extract typed output for a specific function call from the request.

    Finds the FunctionCallOutputItem matching call.call_id in the request's input,
    parses its structuredContent as output_type.

    Args:
        req: The request containing tool outputs
        call: The FunctionCallItem whose output to extract
        output_type: Pydantic model type for the output

    Returns:
        Parsed output as output_type

    Raises:
        ValueError: If not exactly one matching output, or if output is not string type
    """
    matches = [item for item in req.input if isinstance(item, FunctionCallOutputItem) and item.call_id == call.call_id]

    if len(matches) == 0:
        raise ValueError(f"No output found for call_id={call.call_id}")
    if len(matches) > 1:
        raise ValueError(f"Multiple outputs found for call_id={call.call_id}: expected exactly 1, got {len(matches)}")

    output = matches[0].output
    # output can be str (JSON) or list[FunctionCallOutputContent]
    if not isinstance(output, str):
        raise ValueError(f"Expected string output for call_id={call.call_id}, got list")

    # OpenAI format returns the structured content directly (not wrapped).
    return TypeAdapter(output_type).validate_python(json.loads(output))


def tool_roundtrip[T: BaseModel](
    call: FunctionCallItem, output_type: type[T]
) -> Generator[FunctionCallItem, ResponsesRequest, T]:
    """Yield tool call, receive response, return typed output."""
    req = yield call
    return extract_call_output(req, call, output_type)


# Type for play() generator function passed to GeneratorMock.mock() decorator
PlayGen = Generator[ResponseOutItem | list[ResponseOutItem] | None, ResponsesRequest]


class GeneratorMock(ItemFactory, OpenAIModelProto):
    """Abstract base for generator-based OpenAI mocks.

    Subclasses ItemFactory for convenient item construction and implements
    OpenAIModelProto for use as a mock client.

    Subclass and override play() to provide the generator.
    """

    _check_consumed: bool = True

    def __init__(self) -> None:
        super().__init__(call_id_prefix="test")
        self._consumed = False
        self.model = "test-model"
        self._gen = self._wrapped_play()
        next(self._gen)  # Prime to first yield

    @abstractmethod
    def play(self) -> PlayGen:
        """Override in subclass to provide the generator."""

    def _wrapped_play(self) -> PlayGen:
        """Wrap play() to track consumption."""
        yield from self.play()
        self._consumed = True

    @property
    def consumed(self) -> bool:
        """True if generator ran to completion."""
        return self._consumed

    def assert_consumed(self) -> None:
        """Assert generator was fully consumed (no more yields pending)."""
        if not self._consumed:
            raise AssertionError("Mock has unconsumed steps - generator did not complete")

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        """Send request to generator, return wrapped response."""
        try:
            result = self._gen.send(req)
        except StopIteration:
            raise RuntimeError("Mock exhausted: generator ended but received another request") from None

        return self._wrap_result(result)

    def _wrap_result(self, result: ResponseOutItem | list[ResponseOutItem] | None) -> ResponsesResult:
        """Auto-wrap yielded values to ResponsesResult."""
        if result is None:
            raise RuntimeError("Generator yielded None when response expected")
        if isinstance(result, list):
            return self._make(*result)
        return self._make(result)

    def _make(self, *items: ResponseOutItem) -> ResponsesResult:
        """Create ResponsesResult from items."""
        out_tokens = sum(max(1, len(it.text)) for it in items if isinstance(it, AssistantMessageOut))
        return ResponsesResult(
            id="resp_generic",
            usage=ResponseUsage(
                input_tokens=0,
                input_tokens_details=InputTokensDetails(cached_tokens=0),
                output_tokens=(1 if out_tokens else 0),
                output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
                total_tokens=(1 if out_tokens else 0),
            ),
            output=list(items),
        )

    def call_roundtrip[S: FastMCP, U: BaseModel](
        self, mounted: Mounted[S], tool: Tool, arguments: BaseModel, output_type: type[U]
    ) -> Generator[FunctionCallItem, ResponsesRequest, U]:
        """Create tool call and yield roundtrip generator."""
        call = self.tool_call(mounted.tool_name(tool), arguments.model_dump(mode="json"))
        return tool_roundtrip(call, output_type)

    def mcp_tool_call(
        self, server: MCPMountPrefix, tool: str, arguments: BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create tool call for MCP server/tool with automatic naming."""
        return self.tool_call(build_mcp_function(server, tool), arguments, call_id)

    def docker_exec_roundtrip(
        self, cmd: list[str], *, timeout_ms: int = 5000, cwd: str | None = None, tool_name: str = "exec"
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Yield docker exec call, receive response, return typed result."""
        exec_input = make_exec_input(cmd, timeout_ms=timeout_ms, cwd=cwd)
        call = self.mcp_tool_call(ContainerExecServer.DOCKER_MOUNT_PREFIX, tool_name, exec_input)
        return tool_roundtrip(call, BaseExecResult)


class DecoratorMock(GeneratorMock):
    """GeneratorMock that delegates play() to a function passed at init.

    Use the @Subclass.mock() decorator pattern:

        @EchoMock.mock()
        def my_mock(m: EchoMock):
            req = yield
            yield from m.echo_roundtrip("hello")

    By default, assert_consumed() is called automatically after the test to
    verify all steps were executed. Use check_consumed=False to disable.
    """

    # play_fn accepts subclass type at runtime, but type system can't express
    # "callable accepting same type as self" with classmethod factory pattern
    _play_fn: Callable[[DecoratorMock], PlayGen]

    def __init__(self, play_fn: Callable[[DecoratorMock], PlayGen], check_consumed: bool = True) -> None:
        self._play_fn = play_fn
        self._check_consumed = check_consumed
        super().__init__()

    def play(self) -> PlayGen:
        return self._play_fn(self)

    @classmethod
    def mock[T: DecoratorMock](
        cls: type[T], *args: object, check_consumed: bool = True, **kwargs: object
    ) -> Callable[[Callable[[T], PlayGen]], T]:
        """Decorator to create mock instance from generator function."""

        def decorator(fn: Callable[[T], PlayGen]) -> T:
            # fn: Callable[[T], ...] stored as Callable[[DecoratorMock], ...] - safe
            # because play() only calls it with self (which is T at runtime)
            return cls(fn, check_consumed, *args, **kwargs)  # type: ignore[arg-type]

        return decorator


class DockerExecMock(DecoratorMock):
    """Mock with docker exec helpers.

    Example:
        @DockerExecMock.mock(runtime)
        def mock(m: DockerExecMock):
            req = yield
            result = yield from m.exec(["ls", "-la"])
            assert result.exit.exit_code == 0
            yield from m.exec(["submit"])
    """

    def __init__(
        self, play_fn: Callable[[DockerExecMock], PlayGen], check_consumed: bool, runtime: Mounted[ContainerExecServer]
    ) -> None:
        self._runtime = runtime
        # Safe: play() only calls play_fn with self which is DockerExecMock
        super().__init__(play_fn, check_consumed)  # type: ignore[arg-type]

    def exec(
        self, cmd: list[str], timeout_ms: int = 30000, cwd: str | None = None
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Yield exec call, receive response, return typed result."""
        return self.call_roundtrip(
            self._runtime,
            self._runtime.server.exec_tool,
            make_exec_input(cmd, timeout_ms=timeout_ms, cwd=cwd),
            BaseExecResult,
        )


class EchoMock(DecoratorMock):
    """Mock with echo server helpers.

    Example:
        @EchoMock.mock()
        def mock(m: EchoMock):
            req = yield
            result = yield from m.echo_roundtrip("hello")
            assert result.echo == "hello"
    """

    def echo_call(self, text: str) -> FunctionCallItem:
        """Create echo tool call item."""
        return self.tool_call(build_mcp_function(ECHO_MOUNT_PREFIX, ECHO_TOOL_NAME), EchoInput(text=text))

    def echo_roundtrip(self, text: str) -> Generator[FunctionCallItem, ResponsesRequest, EchoOutput]:
        """Yield echo call, receive response, return typed result."""
        return tool_roundtrip(self.echo_call(text), EchoOutput)


# ---- Pytest fixtures ----


@pytest.fixture(scope="session")
def reasoning_model() -> str:
    """Default reasoning-capable model for adapter fixtures.

    Tests may override via RESPONSES_TEST_MODEL env.
    """
    return os.environ.get("RESPONSES_TEST_MODEL", "gpt-5-nano")


@pytest.fixture(scope="session")
def responses_factory(reasoning_model: str) -> ResponsesFactory:
    return ResponsesFactory(reasoning_model)

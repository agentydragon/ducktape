"""Response factories and mock runners for agent tests (non-MCP).

Provides declarative test response building:
- ResponsesFactory: Builds mock ResponsesResult objects
- GeneratorRunner + openai_mock: Function-based generator mocks
- GeneratorMock + DecoratorMock: Class-based generator mocks
- extract_call_output / tool_roundtrip: Typed tool output extraction
"""

from __future__ import annotations

import json
import logging
import os
from abc import abstractmethod
from collections.abc import Callable, Generator
from typing import Any

import pytest
from pydantic import TypeAdapter

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
    For MCP-aware methods (mcp_tool_call, docker_exec, mounted_tool_call),
    use MCPResponsesFactory from agent_core.testing.mcp.responses.
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


# Type for generator that yields responses and receives requests
MockScriptGen = Generator[ResponsesResult | ResponseOutItem | list[ResponseOutItem] | None, ResponsesRequest]


class GeneratorRunner(OpenAIModelProto):
    """Generator-based OpenAI mock with runtime 1:1 request-response enforcement.

    Use via the @openai_mock decorator for concise test code:

        @openai_mock
        def mock(factory):
            req = yield  # Receive first request
            call = factory.tool_call("my_tool", {"arg": "val"})
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
            call = factory.tool_call("my_tool", {"arg": "val"})
            req = yield call
            yield factory.assistant_text("Done")
    """
    factory = ResponsesFactory("test-model")
    gen = fn(factory)
    return GeneratorRunner(gen, factory)


def extract_call_output[T](req: ResponsesRequest, call: FunctionCallItem, output_type: type[T]) -> T:
    """Extract typed output for a specific function call from the request.

    Finds the FunctionCallOutputItem matching call.call_id in the request's input,
    parses its output as output_type.
    """
    matches = [item for item in req.input if isinstance(item, FunctionCallOutputItem) and item.call_id == call.call_id]

    if len(matches) == 0:
        raise ValueError(f"No output found for call_id={call.call_id}")
    if len(matches) > 1:
        raise ValueError(f"Multiple outputs found for call_id={call.call_id}: expected exactly 1, got {len(matches)}")

    return TypeAdapter(output_type).validate_python(json.loads(matches[0].output))


def tool_roundtrip[T](call: FunctionCallItem, output_type: type[T]) -> Generator[FunctionCallItem, ResponsesRequest, T]:
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
    For MCP-aware methods (call_roundtrip, mcp_tool_call),
    use MCPDecoratorMock from agent_core.testing.mcp.responses.
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


class DecoratorMock(GeneratorMock):
    """GeneratorMock that delegates play() to a function passed at init.

    Use the @Subclass.mock() decorator pattern:

        @DecoratorMock.mock()
        def my_mock(m: DecoratorMock):
            req = yield
            yield m.assistant_text("Done")

    By default, assert_consumed() is called automatically after the test to
    verify all steps were executed. Use check_consumed=False to disable.
    """

    # play_fn accepts subclass type at runtime, but type system can't express
    # "callable accepting same type as self" with classmethod factory pattern
    _play_fn: Callable[[DecoratorMock], PlayGen]

    def __init__(self, play_fn: Callable[[DecoratorMock], PlayGen], *, check_consumed: bool = True) -> None:
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


# ---- Pytest fixtures ----


@pytest.fixture(scope="session")
def reasoning_model() -> str:
    """Default reasoning-capable model for adapter fixtures.

    Tests may override via RESPONSES_TEST_MODEL env.
    """
    return os.environ.get("RESPONSES_TEST_MODEL", "gpt-5-nano")


# responses_factory fixture lives in agent_core.testing.mcp.responses (returns MCPResponsesFactory)

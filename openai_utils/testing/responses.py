"""Shared model-response factories and mock runners for tests.

Provides shared test response building:
- ItemFactory: Builds adapter items (tool calls, assistant text, reasoning)
- ResponsesFactory: Builds mock ResponsesResult objects
- GeneratorMock + DecoratorMock: Class-based generator mocks
- extract_call_output / tool_roundtrip: Typed tool output extraction
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Callable, Generator
from typing import Any

import pydantic_core
from pydantic import BaseModel, TypeAdapter

from openai_utils.model import (
    AssistantMessageOut,
    FunctionCallItem,
    FunctionCallOutputItem,
    InputTokensDetails,
    Model,
    OutputText,
    OutputTokensDetails,
    ReasoningItem,
    ResponseOutItem,
    ResponsesRequest,
    ResponsesResult,
    ResponseUsage,
)

logger = logging.getLogger(__name__)


def _make_response(*items: ResponseOutItem) -> ResponsesResult:
    out_tokens = sum(max(1, len(it.text)) for it in items if isinstance(it, AssistantMessageOut))
    return ResponsesResult(
        id="resp_generic",
        usage=ResponseUsage(
            input_tokens=0,
            input_tokens_details=InputTokensDetails(cached_tokens=0, cache_write_tokens=0),
            output_tokens=(1 if out_tokens else 0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
            total_tokens=(1 if out_tokens else 0),
        ),
        output=list(items),
    )


class ItemFactory:
    """Helper for constructing adapter items with convenient ID handling.

    Production reasoning items originate from the model; `make_item_reasoning` exists for mocks that synthesize them.
    """

    def __init__(self, call_id_prefix: str = "bootstrap") -> None:
        self._call_id_seq = 0
        self._reasoning_seq = 0
        self._prefix = call_id_prefix

    def next_call_id(self) -> str:
        self._call_id_seq += 1
        return f"{self._prefix}:{self._call_id_seq}"

    def _next_reasoning_id(self) -> int:
        self._reasoning_seq += 1
        return self._reasoning_seq

    def tool_call(
        self, name: str, arguments: dict[str, Any] | BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        cid = call_id or self.next_call_id()
        args_dict = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
        return FunctionCallItem(
            call_id=cid, name=name, arguments=pydantic_core.to_json(args_dict, fallback=str).decode("utf-8")
        )

    def assistant_text(self, text: str) -> AssistantMessageOut:
        return AssistantMessageOut(content=[OutputText(text=text)])

    def make_item_reasoning(self, id: str | None = None) -> ReasoningItem:
        return ReasoningItem(id=id or f"rs_{self._next_reasoning_id()}")


class ResponsesFactory(ItemFactory):
    """Convenience adapter response builders.

    Provides methods to build mock ResponsesResult objects for testing.
    """

    def __init__(self) -> None:
        super().__init__(call_id_prefix="test")

    def make(self, *items: ResponseOutItem) -> ResponsesResult:
        return _make_response(*items)


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

    return TypeAdapter(output_type).validate_json(matches[0].output)


def tool_roundtrip[T](call: FunctionCallItem, output_type: type[T]) -> Generator[FunctionCallItem, ResponsesRequest, T]:
    """Yield tool call, receive response, return typed output."""
    req = yield call
    return extract_call_output(req, call, output_type)


# Type for play() generator function passed to GeneratorMock.mock() decorator
PlayGen = Generator[ResponseOutItem | list[ResponseOutItem] | None, ResponsesRequest]


class GeneratorMock(ItemFactory, Model):
    """Abstract base for generator-based OpenAI mocks.

    Subclasses ItemFactory for convenient item construction and implements
    Model for use as a mock client.

    Subclass and override play() to provide the generator.
    """

    model = "test-model"

    def __init__(self) -> None:
        super().__init__(call_id_prefix="test")
        self._gen = self.play()
        next(self._gen)  # Prime to first yield

    @abstractmethod
    def play(self) -> PlayGen:
        """Override in subclass to provide the generator."""

    async def sample(self, req: ResponsesRequest) -> ResponsesResult:
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
            return _make_response(*result)
        return _make_response(result)


class DecoratorMock(GeneratorMock):
    """GeneratorMock that delegates play() to a function passed at init.

    Use the @Subclass.mock() decorator pattern:

        @DecoratorMock.mock()
        def my_mock(m: DecoratorMock):
            req = yield
            yield m.assistant_text("Done")

    """

    # play_fn accepts subclass type at runtime, but type system can't express
    # "callable accepting same type as self" with classmethod factory pattern
    _play_fn: Callable[[DecoratorMock], PlayGen]

    def __init__(self, play_fn: Callable[[DecoratorMock], PlayGen]) -> None:
        self._play_fn = play_fn
        super().__init__()

    def play(self) -> PlayGen:
        return self._play_fn(self)

    @classmethod
    def mock[T: DecoratorMock](cls: type[T], *args: object, **kwargs: object) -> Callable[[Callable[[T], PlayGen]], T]:
        """Decorator to create mock instance from generator function."""

        def decorator(fn: Callable[[T], PlayGen]) -> T:
            # fn: Callable[[T], ...] stored as Callable[[DecoratorMock], ...] - safe
            # because play() only calls it with self (which is T at runtime)
            return cls(fn, *args, **kwargs)  # type: ignore[arg-type]

        return decorator

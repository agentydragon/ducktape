"""Generator-based response mocks and typed tool round trips for tests."""

from __future__ import annotations

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


PlayGen = Generator[ResponseOutItem | list[ResponseOutItem] | None, ResponsesRequest]


class DecoratorMock(Model):
    """Turn a test generator into a model that receives requests and yields response items.

    The first yield primes the generator. Each later yield supplies the next response.

    Example:
        @DecoratorMock.mock()
        def my_mock(m: DecoratorMock):
            req = yield
            yield m.assistant_text("Done")
    """

    model = "test-model"

    def __init__(self, play_fn: Callable[[DecoratorMock], PlayGen]) -> None:
        self._call_id_seq = 0
        self._reasoning_seq = 0
        self._gen = play_fn(self)
        next(self._gen)

    @classmethod
    def mock[T: DecoratorMock](cls: type[T]) -> Callable[[Callable[[T], PlayGen]], T]:
        """Decorate a generator that receives an instance of this mock class."""

        def decorator(fn: Callable[[T], PlayGen]) -> T:
            return cls(fn)  # type: ignore[arg-type]  # The constructor calls fn with a T.

        return decorator

    def tool_call(
        self, name: str, arguments: dict[str, Any] | BaseModel, call_id: str | None = None
    ) -> FunctionCallItem:
        if not call_id:
            self._call_id_seq += 1
            call_id = f"test:{self._call_id_seq}"
        args_dict = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
        return FunctionCallItem(
            call_id=call_id, name=name, arguments=pydantic_core.to_json(args_dict, fallback=str).decode("utf-8")
        )

    def assistant_text(self, text: str) -> AssistantMessageOut:
        return AssistantMessageOut(content=[OutputText(text=text)])

    def make_item_reasoning(self, id: str | None = None) -> ReasoningItem:
        if not id:
            self._reasoning_seq += 1
            id = f"rs_{self._reasoning_seq}"
        return ReasoningItem(id=id)

    async def sample(self, req: ResponsesRequest) -> ResponsesResult:
        try:
            result = self._gen.send(req)
        except StopIteration:
            raise RuntimeError("Mock exhausted: generator ended but received another request") from None

        if result is None:
            raise RuntimeError("Generator yielded None when response expected")
        if isinstance(result, list):
            return _make_response(*result)
        return _make_response(result)

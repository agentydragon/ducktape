"""Mock OpenAI clients for testing.

Provides mock implementations of OpenAIModelProto for unit tests:
- FakeOpenAIModel: Returns predefined responses in sequence
- CapturingOpenAIModel: Wraps any model and records all requests
- LIVE sentinel: Used in parametrized tests to select real API
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from openai_utils.model import OpenAIModelProto, ResponsesRequest, ResponsesResult

# Sentinel for selecting a real AsyncOpenAI client in parameterized tests
LIVE = object()

# Function type for a mocked OpenAI Responses API call
ResponsesCreateFn = Callable[[ResponsesRequest], Awaitable[ResponsesResult]]


class CapturingOpenAIModel(OpenAIModelProto):
    """Wrapper that captures all requests to an underlying OpenAI model.

    Wraps any OpenAIModelProto and records all requests in the .captured list.
    """

    def __init__(self, inner: OpenAIModelProto) -> None:
        self._inner = inner
        self.model = inner.model
        self.captured: list[ResponsesRequest] = []
        self.calls = 0

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        self.captured.append(req)
        self.calls += 1
        return await self._inner.responses_create(req)


class CapturedRequest(BaseModel):
    """Pydantic wrapper for captured Responses.create kwargs used in tests."""

    input: list[dict[str, Any]] | None = None
    model_config = ConfigDict(extra="allow")


class FakeOpenAIModel(OpenAIModelProto):
    """Mock OpenAI model that returns predefined responses.

    Wrap with CapturingOpenAIModel if you need to inspect requests.
    """

    def __init__(self, outputs: list[ResponsesResult] | tuple[ResponsesResult, ...]) -> None:
        self._outputs: list[ResponsesResult] = list(outputs)
        self.calls = 0
        self.model = "fake-model"

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        if not isinstance(req, ResponsesRequest):
            raise TypeError("responses_create expects a ResponsesRequest instance")
        if self.calls >= len(self._outputs):
            last_messages = []
            if isinstance(req.input, list):
                for item in req.input[-2:]:
                    item_dict = item.model_dump()
                    role = item_dict.get("role", str(type(item).__name__))
                    content = str(item_dict)
                    if len(content) > 500:
                        content = content[:500] + "..."
                    last_messages.append(f"  {role}: {content}")
            elif isinstance(req.input, str):
                last_messages.append(f"  (string input): {req.input[:500]}")

            msg_preview = "\n".join(last_messages) if last_messages else "(no messages)"
            raise RuntimeError(
                f"Mock exhausted: {self.calls} calls made but only {len(self._outputs)} responses provided. "
                f"Add more mock responses or reduce max_turns.\n\nLast 2 messages in request:\n{msg_preview}"
            )
        result = self._outputs[self.calls]
        self.calls += 1
        return result


class OpenAIClient(Protocol):
    @property
    def responses(self) -> Any: ...


def make_mock(responses_create_fn: ResponsesCreateFn) -> CapturingOpenAIModel:
    """Construct a capturing mock client that delegates to the provided behavior."""

    class _MockClient(OpenAIModelProto):
        def __init__(self) -> None:
            self.model = "test-model"

        async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
            return await responses_create_fn(req)

    return CapturingOpenAIModel(_MockClient())


class NoopOpenAIClient(OpenAIModelProto):
    """No-op OpenAI client for tests that bypass sampling via SyntheticAction."""

    def __init__(self) -> None:
        self.model = "noop-model"

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        raise NotImplementedError("NoopOpenAIClient should not be called in SyntheticAction path")

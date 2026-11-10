"""Mock OpenAI clients for testing.

Provides mock implementations of Model for unit tests:
- FakeOpenAIModel: Returns predefined responses in sequence
- CapturingOpenAIModel: Wraps a Responses model and records its requests
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from openai_utils.model import Model, ResponsesRequest, ResponsesResult, WireRequest

# Function type for a mocked OpenAI Responses API call
ResponsesCreateFn = Callable[[ResponsesRequest], Awaitable[ResponsesResult]]


class CapturingOpenAIModel(Model):
    """Wrapper that captures all requests to an underlying OpenAI model.

    Wraps a Responses model and records all requests in the .captured list.
    """

    def __init__(self, inner: Model) -> None:
        self._inner = inner
        self.captured: list[ResponsesRequest] = []
        self.calls = 0

    @property
    def model(self) -> str:
        return self._inner.model

    def prepare(self, request: ResponsesRequest) -> WireRequest:
        return self._inner.prepare(request)

    async def sample(self, req: WireRequest) -> ResponsesResult:
        assert isinstance(req, ResponsesRequest)
        self.captured.append(req)
        self.calls += 1
        return await self._inner.sample(req)


class FakeOpenAIModel(Model):
    """Mock OpenAI model that returns predefined responses.

    Wrap with CapturingOpenAIModel if you need to inspect requests.
    """

    model = "fake-model"

    def __init__(self, outputs: list[ResponsesResult] | tuple[ResponsesResult, ...]) -> None:
        self._outputs: list[ResponsesResult] = list(outputs)
        self.calls = 0

    async def sample(self, req: WireRequest) -> ResponsesResult:
        if not isinstance(req, ResponsesRequest):
            raise TypeError("FakeOpenAIModel expects a Responses request")
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


def make_mock(responses_create_fn: ResponsesCreateFn) -> CapturingOpenAIModel:
    """Construct a capturing mock client that delegates to the provided behavior."""

    class _MockClient(Model):
        model = "test-model"

        async def sample(self, req: WireRequest) -> ResponsesResult:
            assert isinstance(req, ResponsesRequest)
            return await responses_create_fn(req)

    return CapturingOpenAIModel(_MockClient())


class NoopOpenAIClient(Model):
    """No-op OpenAI client for tests that bypass sampling via SyntheticAction."""

    model = "noop-model"

    async def sample(self, req: WireRequest) -> ResponsesResult:
        assert isinstance(req, ResponsesRequest)
        raise NotImplementedError("NoopOpenAIClient should not be called in SyntheticAction path")

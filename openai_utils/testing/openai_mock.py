"""Fixed-response and no-op OpenAI test models."""

from __future__ import annotations

from openai_utils.model import Model, ResponsesRequest, ResponsesResult


class FakeOpenAIModel(Model):
    """Mock OpenAI model that returns predefined responses."""

    model = "fake-model"

    def __init__(self, outputs: list[ResponsesResult] | tuple[ResponsesResult, ...]) -> None:
        self._outputs: list[ResponsesResult] = list(outputs)
        self.calls = 0

    async def sample(self, req: ResponsesRequest) -> ResponsesResult:
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


class NoopOpenAIClient(Model):
    """No-op OpenAI client for tests that bypass sampling via SyntheticAction."""

    model = "noop-model"

    async def sample(self, req: ResponsesRequest) -> ResponsesResult:
        raise NotImplementedError("NoopOpenAIClient should not be called in SyntheticAction path")

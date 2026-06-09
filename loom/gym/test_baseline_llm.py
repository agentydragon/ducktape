from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import pytest_bazel
from pydantic import ValidationError

from loom.gym.baseline_llm import LANGFUSE_TAG, ChatEndpoint, answer_tool_schema, build_prompt, forecast, parse_answer
from loom.gym.scoring import QUANTILE_LEVELS, BinaryAnswer, QuantileAnswer
from loom.gym.task import BinaryOutcome, BinaryQuestion, ScalarOutcome, ScalarQuestion, Task

BINARY_TASK = Task(
    task_id="binary-task",
    as_of=date(2024, 7, 1),
    resolution_date=date(2024, 12, 31),
    question=BinaryQuestion(text="Will the S&P 500 close above 6000 by 2024-12-31?"),
    outcome=BinaryOutcome(value=True),
    outcome_source="test fixture",
)

SCALAR_TASK = Task(
    task_id="scalar-task",
    as_of=date(2024, 7, 1),
    resolution_date=date(2024, 12, 31),
    question=ScalarQuestion(text="What will the S&P 500 close at on 2024-12-31?", unit="index points"),
    outcome=ScalarOutcome(value=5881.63),
    outcome_source="test fixture",
)

ENDPOINT = ChatEndpoint(
    base_url="https://litellm.test", api_key="test-key", model_id="glm-4.5", endpoint_model="glm-4.5-anthropic"
)


def test_build_prompt_states_cutoff_and_answer_shape() -> None:
    binary_prompt = build_prompt(BINARY_TASK)
    assert "as of 2024-07-01" in binary_prompt
    assert "submit_answer" in binary_prompt
    scalar_prompt = build_prompt(SCALAR_TASK)
    assert "index points" in scalar_prompt
    for level in QUANTILE_LEVELS:
        assert f'"{level}"' in scalar_prompt


def test_answer_tool_schema_shapes() -> None:
    binary_schema = answer_tool_schema(BINARY_TASK)
    assert binary_schema["required"] == ["p"]
    scalar_schema = answer_tool_schema(SCALAR_TASK)
    quantiles = scalar_schema["properties"]["quantiles"]  # type: ignore[index]
    assert quantiles["required"] == [str(level) for level in QUANTILE_LEVELS]


def _tool_use_response(tool_input: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "submit_answer", "input": tool_input}]})


async def test_forecast_binary_via_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://litellm.test/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["x-litellm-tags"] == LANGFUSE_TAG
        payload = json.loads(request.content)
        assert payload["model"] == "glm-4.5-anthropic"
        assert payload["tool_choice"] == {"type": "tool", "name": "submit_answer"}
        return _tool_use_response({"p": 0.7})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await forecast(client, ENDPOINT, BINARY_TASK) == BinaryAnswer(p=0.7)


async def test_forecast_scalar_via_mock_transport() -> None:
    quantiles = {"0.1": 5000, "0.25": 5300, "0.5": 5600, "0.75": 5900, "0.9": 6200}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _tool_use_response({"quantiles": quantiles}))
    ) as client:
        answer = await forecast(client, ENDPOINT, SCALAR_TASK)
    assert answer == QuantileAnswer(quantiles={0.1: 5000, 0.25: 5300, 0.5: 5600, 0.75: 5900, 0.9: 6200})


async def test_forecast_retries_transient_429() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "throttled"})
        return _tool_use_response({"p": 0.5})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await forecast(client, ENDPOINT, BINARY_TASK) == BinaryAnswer(p=0.5)
    assert calls == 2


def test_parse_answer_rejects_malformed_input() -> None:
    with pytest.raises(ValidationError):
        parse_answer(BINARY_TASK, {"p": 1.5})
    with pytest.raises(ValidationError):
        parse_answer(SCALAR_TASK, {"quantiles": {"0.9": 1.0, "0.1": 5.0}})


if __name__ == "__main__":
    pytest_bazel.main()

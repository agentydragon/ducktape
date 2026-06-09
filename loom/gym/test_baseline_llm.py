from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import pytest_bazel
from pydantic import ValidationError

from loom.gym.baseline_llm import ChatEndpoint, build_prompt, forecast, parse_answer
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


def test_build_prompt_states_cutoff_and_answer_shape() -> None:
    binary_prompt = build_prompt(BINARY_TASK)
    assert "as of 2024-07-01" in binary_prompt
    assert '"p"' in binary_prompt
    scalar_prompt = build_prompt(SCALAR_TASK)
    assert "index points" in scalar_prompt
    for level in QUANTILE_LEVELS:
        assert f'"{level}"' in scalar_prompt


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_forecast_binary_via_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://example.test/v4/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content)["model"] == "glm-4.5"
        return _chat_response('{"p": 0.7}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        endpoint = ChatEndpoint(base_url="https://example.test/v4", api_key="test-key", model_id="glm-4.5")
        assert await forecast(client, endpoint, BINARY_TASK) == BinaryAnswer(p=0.7)


async def test_forecast_scalar_via_mock_transport() -> None:
    quantiles = {"0.1": 5000, "0.25": 5300, "0.5": 5600, "0.75": 5900, "0.9": 6200}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _chat_response(json.dumps({"quantiles": quantiles})))
    ) as client:
        endpoint = ChatEndpoint(base_url="https://example.test/v4", api_key="test-key", model_id="glm-4.5")
        answer = await forecast(client, endpoint, SCALAR_TASK)
    assert answer == QuantileAnswer(quantiles={0.1: 5000, 0.25: 5300, 0.5: 5600, 0.75: 5900, 0.9: 6200})


def test_parse_answer_rejects_malformed_content() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_answer(BINARY_TASK, "not json")
    with pytest.raises(ValidationError):
        parse_answer(BINARY_TASK, '{"p": 1.5}')
    with pytest.raises(ValidationError):
        parse_answer(SCALAR_TASK, '{"quantiles": {"0.9": 1.0, "0.1": 5.0}}')


if __name__ == "__main__":
    pytest_bazel.main()

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import pytest_bazel
from pydantic import ValidationError

from loom.gym.baseline_llm import (
    LANGFUSE_TAG,
    ChatEndpoint,
    build_bundle_prompt,
    build_prompt,
    forecast,
    forecast_bundle,
    parse_answer,
    question_schema,
)
from loom.gym.scoring import QUANTILE_LEVELS, BinaryAnswer, CategoricalAnswer, QuantileAnswer
from loom.gym.task import (
    BinaryOutcome,
    BinaryQuestion,
    CategoricalOutcome,
    CategoricalQuestion,
    EvidenceItem,
    ScalarOutcome,
    ScalarQuestion,
    Task,
)

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

CATEGORICAL_TASK = Task(
    task_id="categorical-task",
    as_of=date(2024, 7, 1),
    resolution_date=date(2024, 12, 31),
    question=CategoricalQuestion(text="Which bucket?", categories=("low", "high"), ordered=True),
    outcome=CategoricalOutcome(category="high"),
    outcome_source="test fixture",
    bundle_id="test-bundle",
)

ENDPOINT = ChatEndpoint(
    base_url="https://litellm.test", api_key="test-key", model_id="glm-4.5", endpoint_model="glm-4.5-anthropic"
)


def test_build_prompt_includes_dossier_files() -> None:
    prompt = build_prompt(BINARY_TASK, dossier={"a.csv": "month,value\n2024-06,1.0\n"})
    assert "--- a.csv ---" in prompt
    assert "2024-06,1.0" in prompt
    assert "--- " not in build_prompt(BINARY_TASK)


def test_build_prompt_states_cutoff_and_answer_shape() -> None:
    binary_prompt = build_prompt(BINARY_TASK)
    assert "as of 2024-07-01" in binary_prompt
    assert "submit_answer" in binary_prompt
    scalar_prompt = build_prompt(SCALAR_TASK)
    assert "index points" in scalar_prompt
    for level in QUANTILE_LEVELS:
        assert f'"{level}"' in scalar_prompt


def test_bundle_prompt_lists_every_sub_question() -> None:
    prompt = build_bundle_prompt([BINARY_TASK, CATEGORICAL_TASK])
    assert "[binary-task]" in prompt
    assert "[categorical-task]" in prompt
    assert "submit_answers" in prompt


EVIDENCE_TASK = BINARY_TASK.model_copy(
    update={
        "evidence": (
            EvidenceItem(
                url="https://example.com/forecast",
                archived_url="https://web.archive.org/web/20240619000000/https://example.com/forecast",
                date=date(2024, 6, 19),
                title="Index forecast roundup",
            ),
        )
    }
)


def test_prompts_render_evidence_only_when_present() -> None:
    prompt = build_prompt(EVIDENCE_TASK)
    assert "URLs that may contain relevant information" in prompt
    # Original URL only — no title (which could carry a curator's framing) and
    # never the pinned archive capture.
    assert "- https://example.com/forecast" in prompt
    assert "Index forecast roundup" not in prompt
    assert "web.archive.org" not in prompt
    assert "URLs that may contain" not in build_prompt(BINARY_TASK)

    bundle_prompt = build_bundle_prompt([EVIDENCE_TASK, CATEGORICAL_TASK])
    assert "  - https://example.com/forecast" in bundle_prompt
    assert bundle_prompt.count("URLs that may contain") == 1


def test_question_schema_shapes() -> None:
    assert question_schema(BINARY_TASK.question)["required"] == ["p"]
    scalar_schema = question_schema(SCALAR_TASK.question)
    quantiles = scalar_schema["properties"]["quantiles"]  # type: ignore[index]
    assert quantiles["required"] == [str(level) for level in QUANTILE_LEVELS]
    categorical_schema = question_schema(CATEGORICAL_TASK.question)
    probabilities = categorical_schema["properties"]["probabilities"]  # type: ignore[index]
    assert probabilities["required"] == ["low", "high"]


def _tool_response(tool_name: str, tool_input: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "tool_use", "name": tool_name, "input": tool_input}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    )


async def test_forecast_binary_via_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://litellm.test/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["x-litellm-tags"] == LANGFUSE_TAG
        payload = json.loads(request.content)
        assert payload["model"] == "glm-4.5-anthropic"
        assert payload["tool_choice"] == {"type": "tool", "name": "submit_answer"}
        return _tool_response("submit_answer", {"p": 0.7})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await forecast(client, ENDPOINT, BINARY_TASK)
    assert result.answers == {"binary-task": BinaryAnswer(p=0.7)}
    assert (result.input_tokens, result.output_tokens) == (100, 20)


async def test_forecast_scalar_via_mock_transport() -> None:
    quantiles = {"0.1": 5000, "0.25": 5300, "0.5": 5600, "0.75": 5900, "0.9": 6200}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _tool_response("submit_answer", {"quantiles": quantiles}))
    ) as client:
        result = await forecast(client, ENDPOINT, SCALAR_TASK)
    assert result.answers == {
        "scalar-task": QuantileAnswer(quantiles={0.1: 5000, 0.25: 5300, 0.5: 5600, 0.75: 5900, 0.9: 6200})
    }


async def test_forecast_bundle_via_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tool_choice"] == {"type": "tool", "name": "submit_answers"}
        schema = payload["tools"][0]["input_schema"]["properties"]["answers"]
        assert schema["required"] == ["binary-task", "categorical-task"]
        return _tool_response(
            "submit_answers",
            {"answers": {"binary-task": {"p": 0.6}, "categorical-task": {"probabilities": {"low": 0.3, "high": 0.7}}}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await forecast_bundle(client, ENDPOINT, [BINARY_TASK, CATEGORICAL_TASK])
    assert result.answers == {
        "binary-task": BinaryAnswer(p=0.6),
        "categorical-task": CategoricalAnswer(probabilities={"low": 0.3, "high": 0.7}),
    }


async def test_forecast_bundle_rejects_mixed_as_of() -> None:
    other = BINARY_TASK.model_copy(update={"as_of": date(2023, 7, 1), "resolution_date": date(2023, 12, 31)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        with pytest.raises(ValueError, match="share as_of"):
            await forecast_bundle(client, ENDPOINT, [BINARY_TASK, other])


async def test_forecast_retries_transient_429() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "throttled"})
        return _tool_response("submit_answer", {"p": 0.5})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await forecast(client, ENDPOINT, BINARY_TASK)
    assert result.answers == {"binary-task": BinaryAnswer(p=0.5)}
    assert calls == 2


def test_parse_answer_rejects_malformed_input() -> None:
    with pytest.raises(ValidationError):
        parse_answer(BINARY_TASK, {"p": 1.5})
    with pytest.raises(ValidationError):
        parse_answer(SCALAR_TASK, {"quantiles": {"0.9": 1.0, "0.1": 5.0}})
    with pytest.raises(ValidationError):
        parse_answer(CATEGORICAL_TASK, {"probabilities": {"low": 0.2, "high": 0.2}})


if __name__ == "__main__":
    pytest_bazel.main()

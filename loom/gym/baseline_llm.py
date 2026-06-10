"""The step-2 baseline contestant: one bare prompt to an LLM, structured answer, no skill material.

Sampling goes through the cluster LiteLLM proxy speaking the **Anthropic
messages API** — the path known to produce reliable structured objects from
the z.ai GLM models — with a forced submit tool call carrying the answer
schema. Every request carries the `loom-gym` tag via `x-litellm-tags`, which
the proxy's Langfuse callback records per trace.

`forecast` asks one task per request; `forecast_bundle` asks a same-`as_of`
group of tasks in one request (one `submit_answers` call keyed by task id) —
the bundle-vs-solo comparison runs on identical tasks, with token usage
captured on every result.

This direct HTTP path is deliberately separate from `inspect_harness.py`:
that one is the tool-using *agent* contestant in the sandbox; this one is the
no-tools baseline it must beat. Both stay.

Anything fancier (skills, tools, mechanistic models) must beat this baseline
on gym loss to justify its existence.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import httpx
from more_itertools import one
from pydantic import BaseModel, ConfigDict, Field, create_model
from tenacity import before_sleep_log, retry, retry_if_exception, stop_after_attempt, wait_exponential

from loom.gym.scoring import QUANTILE_LEVELS, Answer, BinaryAnswer, CategoricalAnswer, QuantileAnswer
from loom.gym.task import BinaryQuestion, CategoricalQuestion, Question, ScalarQuestion, Task

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = "https://litellm.allegedly.works"
LANGFUSE_TAG = "loom-gym"

_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _is_transient(error: BaseException) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code in _TRANSIENT_STATUS


@dataclass(frozen=True)
class ChatEndpoint:
    base_url: str
    api_key: str
    model_id: str
    endpoint_model: str


@dataclass(frozen=True)
class ForecastResult:
    answers: dict[str, Answer]
    input_tokens: int
    output_tokens: int


# extra="forbid" closes every object (additionalProperties: false in the schema);
# the per-question required keys (the fixed quantile levels, the question's
# category set) are real fields, so the Anthropic-shaped API enforces exactly the
# answer shape. Semantic checks (quantiles non-decreasing, probabilities sum to 1)
# stay on the scoring Answer models that parse_answer builds from a validated input.
_FORBID = ConfigDict(extra="forbid")


def _model(name: str, **fields: Any) -> type[BaseModel]:
    # **fields: Any so pydantic's loose create_model overload accepts the
    # (type, FieldInfo) tuples (it types field_definitions as Any | tuple[str, Any]).
    return create_model(name, __config__=_FORBID, **fields)


def _answer_input_model(question: Question) -> type[BaseModel]:
    """Per-question model that is the single source for both the submit tool's
    input schema (`question_schema`) and the shape validation of the model's tool
    call (`parse_answer`). Nested objects use aliased fields so the JSON keys are
    the quantile levels / category names."""
    match question:
        case BinaryQuestion():
            p = Field(gt=0, lt=1, description="Probability the question resolves YES, strictly in (0, 1).")
            return _model("BinaryAnswerInput", p=(float, p))
        case ScalarQuestion(unit=unit):
            quantiles = _model(
                "QuantilesInput",
                **{
                    f"q{index}": (float, Field(alias=str(level), description=f"value at the {level} quantile"))
                    for index, level in enumerate(QUANTILE_LEVELS)
                },
            )
            description = f"your value (in {unit}) at each quantile level, non-decreasing in level"
            return _model("ScalarAnswerInput", quantiles=(quantiles, Field(description=description)))
        case CategoricalQuestion(categories=categories):
            probabilities = _model(
                "ProbabilitiesInput",
                **{
                    f"c{index}": (float, Field(alias=category, ge=0, description=f"probability of {category!r}"))
                    for index, category in enumerate(categories)
                },
            )
            description = "probability for each category; values must sum to 1"
            return _model("CategoricalAnswerInput", probabilities=(probabilities, Field(description=description)))


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline Pydantic's `$defs`/`$ref` into one self-contained schema (merging
    sibling keys like a field description over the referenced def). inspect's
    JSONSchema and a plain Anthropic `input_schema` don't resolve `$ref`, so a
    nested model would otherwise lose its structure."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].rsplit("/", 1)[-1]]
                return walk({**target, **{key: value for key, value in node.items() if key != "$ref"}})
            if "allOf" in node and len(node["allOf"]) == 1:
                merged = node["allOf"][0] | {key: value for key, value in node.items() if key != "allOf"}
                return walk(merged)
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return cast("dict[str, Any]", walk(schema))


def question_schema(question: Question) -> dict[str, object]:
    """JSON schema for the answer to `question`, derived from its input model."""
    return _inline_refs(_answer_input_model(question).model_json_schema())


def answer_instruction(question: Question) -> str:
    match question:
        case BinaryQuestion():
            return "answer with p = your probability that the question resolves YES"
        case ScalarQuestion(unit=unit):
            levels = ", ".join(f'"{level}"' for level in QUANTILE_LEVELS)
            return f"answer with your {levels} quantiles for the value in {unit}, non-decreasing in level"
        case CategoricalQuestion():
            return "answer with your probability for each listed category; probabilities must sum to 1"


def parse_answer(task: Task, tool_input: dict[str, object]) -> Answer:
    # Validate the strict per-question shape via the same model the schema came
    # from, then build the scoring Answer (which adds the semantic checks).
    # by_alias dumps back to the wire keys (quantile levels / category names).
    data = _answer_input_model(task.question).model_validate(tool_input).model_dump(by_alias=True)
    match task.question:
        case BinaryQuestion():
            return BinaryAnswer.model_validate(data)
        case ScalarQuestion():
            return QuantileAnswer.model_validate(data)
        case CategoricalQuestion():
            return CategoricalAnswer.model_validate(data)


def _as_of_header(task: Task, dossier: dict[str, str] | None) -> str:
    data_section = ""
    if dossier is not None:
        blocks = "\n".join(f"--- {name} ---\n{content}" for name, content in sorted(dossier.items()))
        data_section = f"You are given these data files:\n{blocks}\n"
    return (
        data_section
        + f"You are forecasting as of {task.as_of}. Use only knowledge of events on or before {task.as_of}; "
        "if you happen to know anything about later events, you must ignore it.\n"
    )


def _evidence_block(task: Task, indent: str = "") -> str:
    """Source-URL lines (no titles), each ending in a newline; empty when the
    task has none. Titles are withheld so the prompt never carries a curator's
    framing of what a source says.
    """
    if not task.evidence:
        return ""
    lines = [
        "URLs that may contain relevant information (published on or before the information cutoff):",
        *(f"- {item.url}" for item in task.evidence),
    ]
    return "".join(f"{indent}{line}\n" for line in lines)


def build_prompt(task: Task, dossier: dict[str, str] | None = None) -> str:
    return (
        _as_of_header(task, dossier)
        + _evidence_block(task)
        + f"Question: {task.question.text}\n"
        + f"Resolution date: {task.resolution_date}\n"
        + f"Call submit_answer: {answer_instruction(task.question)}."
    )


def build_bundle_prompt(tasks: Sequence[Task], dossier: dict[str, str] | None = None) -> str:
    lines = [_as_of_header(tasks[0], dossier), "Sub-questions:"]
    for task in tasks:
        lines.append(f"[{task.task_id}] {task.question.text} (resolves {task.resolution_date})")
        if evidence_block := _evidence_block(task, indent="  "):
            lines.append(evidence_block.rstrip("\n"))
        lines.append(f"  → {answer_instruction(task.question)}")
    lines.append("Call submit_answers once, with an answer for every sub-question id.")
    return "\n".join(lines)


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, max=8),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _post_with_retry(
    client: httpx.AsyncClient, endpoint: ChatEndpoint, payload: dict[str, object]
) -> httpx.Response:
    headers = {"x-api-key": endpoint.api_key, "anthropic-version": "2023-06-01", "x-litellm-tags": LANGFUSE_TAG}
    response = await client.post(f"{endpoint.base_url}/v1/messages", headers=headers, json=payload, timeout=300.0)
    response.raise_for_status()
    return response


def _tool_use_input(response: httpx.Response) -> tuple[dict[str, object], int, int]:
    body = response.json()
    tool_use = one(block for block in body["content"] if block["type"] == "tool_use")
    usage = body.get("usage", {})
    return tool_use["input"], usage.get("input_tokens", 0), usage.get("output_tokens", 0)


async def forecast(
    client: httpx.AsyncClient, endpoint: ChatEndpoint, task: Task, dossier: dict[str, str] | None = None
) -> ForecastResult:
    payload = {
        "model": endpoint.endpoint_model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": build_prompt(task, dossier)}],
        "tools": [
            {
                "name": "submit_answer",
                "description": "Submit your forecast.",
                "input_schema": question_schema(task.question),
            }
        ],
        "tool_choice": {"type": "tool", "name": "submit_answer"},
    }
    tool_input, input_tokens, output_tokens = _tool_use_input(await _post_with_retry(client, endpoint, payload))
    logger.info("forecast %s: %s", task.task_id, tool_input)
    return ForecastResult(
        answers={task.task_id: parse_answer(task, tool_input)}, input_tokens=input_tokens, output_tokens=output_tokens
    )


async def forecast_bundle(
    client: httpx.AsyncClient, endpoint: ChatEndpoint, tasks: Sequence[Task], dossier: dict[str, str] | None = None
) -> ForecastResult:
    if len({task.as_of for task in tasks}) != 1:
        raise ValueError(f"bundle tasks must share as_of: {sorted({task.as_of for task in tasks})=}")
    payload = {
        "model": endpoint.endpoint_model,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": build_bundle_prompt(tasks, dossier)}],
        "tools": [
            {
                "name": "submit_answers",
                "description": "Submit your forecast for every sub-question.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "answers": {
                            "type": "object",
                            "properties": {task.task_id: question_schema(task.question) for task in tasks},
                            "required": [task.task_id for task in tasks],
                            "additionalProperties": False,
                        }
                    },
                    "required": ["answers"],
                    "additionalProperties": False,
                },
            }
        ],
        "tool_choice": {"type": "tool", "name": "submit_answers"},
    }
    label = f"bundle:{tasks[0].bundle_id or tasks[0].task_id}"
    tool_input, input_tokens, output_tokens = _tool_use_input(await _post_with_retry(client, endpoint, payload))
    logger.info("forecast %s: %s", label, tool_input)
    submitted = tool_input["answers"]
    if not isinstance(submitted, dict):
        raise ValueError(f"submit_answers payload is not an object: {type(submitted)=}")
    return ForecastResult(
        answers={task.task_id: parse_answer(task, submitted[task.task_id]) for task in tasks},
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

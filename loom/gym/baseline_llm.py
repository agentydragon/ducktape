"""The step-2 baseline contestant: one bare prompt to an LLM, structured answer, no skill material.

Sampling goes through the cluster LiteLLM proxy speaking the **Anthropic
messages API** — the path known to produce reliable structured objects from
the z.ai GLM models — with a forced `submit_answer` tool call carrying the
answer schema. Every request carries the `loom-gym` tag via `x-litellm-tags`,
which the proxy's Langfuse callback records per trace.

Anything fancier (skills, tools, mechanistic models) must beat this baseline
on gym loss to justify its existence.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from more_itertools import one

from loom.gym.scoring import QUANTILE_LEVELS, Answer, BinaryAnswer, QuantileAnswer
from loom.gym.task import BinaryQuestion, ScalarQuestion, Task

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = "https://litellm.allegedly.works"
LANGFUSE_TAG = "loom-gym"

_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_ATTEMPTS = 4


@dataclass(frozen=True)
class ChatEndpoint:
    base_url: str
    api_key: str
    model_id: str
    endpoint_model: str


def build_prompt(task: Task, dossier: dict[str, str] | None = None) -> str:
    data_section = ""
    if dossier is not None:
        blocks = "\n".join(f"--- {name} ---\n{content}" for name, content in sorted(dossier.items()))
        data_section = f"You are given these data files:\n{blocks}\n"
    header = (
        data_section
        + f"You are forecasting as of {task.as_of}. Use only knowledge of events on or before {task.as_of}; "
        "if you happen to know anything about later events, you must ignore it.\n"
        f"Question: {task.question.text}\n"
        f"Resolution date: {task.resolution_date}\n"
    )
    match task.question:
        case BinaryQuestion():
            return header + "Call submit_answer with p = your probability that the question resolves YES."
        case ScalarQuestion(unit=unit):
            levels = ", ".join(f'"{level}"' for level in QUANTILE_LEVELS)
            return (
                header
                + f"Call submit_answer with your {levels} quantiles for the value in {unit}. "
                + "Values must be non-decreasing in level."
            )


def answer_tool_schema(task: Task) -> dict[str, object]:
    match task.question:
        case BinaryQuestion():
            return {
                "type": "object",
                "properties": {"p": {"type": "number", "minimum": 0, "maximum": 1}},
                "required": ["p"],
                "additionalProperties": False,
            }
        case ScalarQuestion():
            return {
                "type": "object",
                "properties": {
                    "quantiles": {
                        "type": "object",
                        "properties": {str(level): {"type": "number"} for level in QUANTILE_LEVELS},
                        "required": [str(level) for level in QUANTILE_LEVELS],
                        "additionalProperties": False,
                    }
                },
                "required": ["quantiles"],
                "additionalProperties": False,
            }


def parse_answer(task: Task, tool_input: dict[str, object]) -> Answer:
    match task.question:
        case BinaryQuestion():
            return BinaryAnswer.model_validate(tool_input)
        case ScalarQuestion():
            return QuantileAnswer.model_validate(tool_input)


async def forecast(
    client: httpx.AsyncClient, endpoint: ChatEndpoint, task: Task, dossier: dict[str, str] | None = None
) -> Answer:
    payload = {
        "model": endpoint.endpoint_model,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": build_prompt(task, dossier)}],
        "tools": [
            {"name": "submit_answer", "description": "Submit your forecast.", "input_schema": answer_tool_schema(task)}
        ],
        "tool_choice": {"type": "tool", "name": "submit_answer"},
    }
    headers = {"x-api-key": endpoint.api_key, "anthropic-version": "2023-06-01", "x-litellm-tags": LANGFUSE_TAG}
    for attempt in range(_ATTEMPTS):
        try:
            response = await client.post(
                f"{endpoint.base_url}/v1/messages", headers=headers, json=payload, timeout=180.0
            )
        except httpx.TransportError as error:
            if attempt == _ATTEMPTS - 1:
                raise
            logger.warning("transient transport error on %s (attempt %d): %s", task.task_id, attempt, error)
            await asyncio.sleep(2.0**attempt)
            continue
        if response.status_code in _TRANSIENT_STATUS and attempt < _ATTEMPTS - 1:
            logger.warning("transient HTTP %d on %s (attempt %d)", response.status_code, task.task_id, attempt)
            await asyncio.sleep(2.0**attempt)
            continue
        response.raise_for_status()
        break
    tool_use = one(block for block in response.json()["content"] if block["type"] == "tool_use")
    logger.info("forecast %s: %s", task.task_id, tool_use["input"])
    return parse_answer(task, tool_use["input"])

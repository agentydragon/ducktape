"""The step-2 baseline contestant: one bare prompt to an LLM, structured answer, no skill material.

Anything fancier (skills, tools, mechanistic models) must beat this on gym loss
to justify its existence. Talks to any OpenAI-compatible chat-completions
endpoint (z.ai for the asserted-cutoff models).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from loom.gym.scoring import QUANTILE_LEVELS, Answer, BinaryAnswer, QuantileAnswer
from loom.gym.task import BinaryQuestion, ScalarQuestion, Task

logger = logging.getLogger(__name__)

# Paid coding-plan tier: dedicated rate limits. The free general tier
# (`/api/paas/v4`) throttles aggressively (429s); see pm_reifier's z.ai notes.
ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"


@dataclass(frozen=True)
class ChatEndpoint:
    base_url: str
    api_key: str
    model_id: str


def build_prompt(task: Task) -> str:
    header = (
        f"You are forecasting as of {task.as_of}. Use only knowledge of events on or before {task.as_of}; "
        "if you happen to know anything about later events, you must ignore it.\n"
        f"Question: {task.question.text}\n"
        f"Resolution date: {task.resolution_date}\n"
    )
    match task.question:
        case BinaryQuestion():
            return header + 'Respond with JSON only: {"p": <probability the question resolves YES, between 0 and 1>}'
        case ScalarQuestion(unit=unit):
            levels = ", ".join(f'"{level}"' for level in QUANTILE_LEVELS)
            return (
                header
                + f'Respond with JSON only: {{"quantiles": {{<level>: <value in {unit}>}}}} '
                + f"with exactly these quantile levels: {levels}. Values must be non-decreasing in level."
            )


def parse_answer(task: Task, content: str) -> Answer:
    data = json.loads(content)
    match task.question:
        case BinaryQuestion():
            return BinaryAnswer.model_validate(data)
        case ScalarQuestion():
            return QuantileAnswer.model_validate(data)


async def forecast(client: httpx.AsyncClient, endpoint: ChatEndpoint, task: Task) -> Answer:
    response = await client.post(
        f"{endpoint.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {endpoint.api_key}"},
        json={
            "model": endpoint.model_id,
            "messages": [{"role": "user", "content": build_prompt(task)}],
            "response_format": {"type": "json_object"},
        },
        timeout=120.0,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    logger.info("forecast %s: %s", task.task_id, content)
    return parse_answer(task, content)

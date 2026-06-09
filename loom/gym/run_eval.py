"""Run the bare-prompt LLM baseline over the gym tasks and print per-task scores.

Usage (key = the LiteLLM master key, mirrored into claude-sandbox as `litellm-master-key`):

    LITELLM_API_KEY=... bazelisk run //loom/gym:baseline_eval -- --model-id glm-4.5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from loom.gym.baseline_llm import LITELLM_BASE_URL, ChatEndpoint, forecast
from loom.gym.model_cutoffs import KNOWN_MODEL_CUTOFFS
from loom.gym.scoring import Answer, TaskScore, score
from loom.gym.series_tasks import all_tasks
from loom.gym.task import Task

logger = logging.getLogger(__name__)

_CONCURRENCY = 4


@dataclass(frozen=True)
class EvalRow:
    task: Task
    answer: Answer
    task_score: TaskScore


def admissible_tasks(model_id: str, task_filter: str | None, strict: bool) -> list[Task]:
    if model_id not in KNOWN_MODEL_CUTOFFS:
        raise ValueError(f"unknown model — add it to KNOWN_MODEL_CUTOFFS with provenance: {model_id=}")
    model_cutoff = KNOWN_MODEL_CUTOFFS[model_id]
    bound = model_cutoff.weights_released if strict else model_cutoff.knowledge_cutoff
    tasks = []
    for task in all_tasks():
        if task_filter is not None and task_filter not in task.task_id:
            continue
        if bound > task.as_of:
            logger.info("skipping %s: model bound %s is after task as_of %s", task.task_id, bound, task.as_of)
            continue
        tasks.append(task)
    return tasks


async def run_forecasts(endpoint: ChatEndpoint, tasks: list[Task]) -> list[EvalRow]:
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def forecast_one(client: httpx.AsyncClient, task: Task) -> EvalRow:
        async with semaphore:
            answer = await forecast(client, endpoint, task)
        return EvalRow(task=task, answer=answer, task_score=score(task, answer))

    async with httpx.AsyncClient() as client:
        return list(await asyncio.gather(*(forecast_one(client, task) for task in tasks)))


def report(rows: list[EvalRow], model_id: str, output: Path | None) -> None:
    for row in rows:
        metrics = " ".join(f"{name}={value:.4f}" for name, value in row.task_score.metrics.items())
        print(f"{row.task.task_id:36} {row.task.question.kind:7} {metrics}")
    for kind in ("binary", "scalar"):
        kind_rows = [row for row in rows if row.task.question.kind == kind]
        if not kind_rows:
            continue
        means = {
            name: sum(row.task_score.metrics[name] for row in kind_rows) / len(kind_rows)
            for name in kind_rows[0].task_score.metrics
        }
        print(f"mean[{kind}] over {len(kind_rows)} tasks: " + " ".join(f"{k}={v:.4f}" for k, v in means.items()))

    if output is not None:
        payload = {
            "model_id": model_id,
            "results": [
                {"task_id": row.task.task_id, "answer": row.answer.model_dump(), "metrics": row.task_score.metrics}
                for row in rows
            ],
        }
        output.write_text(json.dumps(payload, indent=2))
        print(f"wrote {output}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True, help="Must be present in KNOWN_MODEL_CUTOFFS.")
    parser.add_argument("--base-url", default=LITELLM_BASE_URL)
    parser.add_argument(
        "--endpoint-model", default=None, help="Model name the endpoint serves; default <model-id>-anthropic."
    )
    parser.add_argument("--api-key-env", default="LITELLM_API_KEY", help="Env var holding the API key.")
    parser.add_argument("--task-filter", default=None, help="Only run tasks whose id contains this substring.")
    parser.add_argument(
        "--strict", action="store_true", help="Bound admissibility by weights-release date, not knowledge cutoff."
    )
    parser.add_argument("--output", type=Path, default=None, help="Optional path for JSON results.")
    args = parser.parse_args()
    api_key = os.environ.get(args.api_key_env) or Path("/tmp/litellm_key").read_text().strip()
    endpoint = ChatEndpoint(
        base_url=args.base_url,
        api_key=api_key,
        model_id=args.model_id,
        endpoint_model=args.endpoint_model or f"{args.model_id}-anthropic",
    )
    tasks = admissible_tasks(model_id=args.model_id, task_filter=args.task_filter, strict=args.strict)
    print(f"{len(tasks)} admissible tasks for {args.model_id} (strict={args.strict})")
    rows = asyncio.run(run_forecasts(endpoint, tasks))
    report(rows=rows, model_id=endpoint.model_id, output=args.output)


if __name__ == "__main__":
    main()

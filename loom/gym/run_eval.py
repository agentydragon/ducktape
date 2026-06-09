"""Run the bare-prompt LLM baseline over the seed tasks and print per-task scores.

Usage (key mirrored from the claude-sandbox `zai-api-key` secret, as in pm_reifier):

    ZAI_API_KEY=... bazelisk run //loom/gym:baseline_eval -- --model-id glm-4.5
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

from loom.gym.baseline_llm import ZAI_BASE_URL, ChatEndpoint, forecast
from loom.gym.model_cutoffs import KNOWN_MODEL_CUTOFFS
from loom.gym.scoring import Answer, TaskScore, score
from loom.gym.seed_tasks import seed_tasks
from loom.gym.task import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvalRow:
    task: Task
    answer: Answer
    task_score: TaskScore


async def run_forecasts(endpoint: ChatEndpoint) -> list[EvalRow]:
    if endpoint.model_id not in KNOWN_MODEL_CUTOFFS:
        raise ValueError(f"unknown model — add it to KNOWN_MODEL_CUTOFFS with provenance: {endpoint.model_id=}")
    model_cutoff = KNOWN_MODEL_CUTOFFS[endpoint.model_id]
    admissible_tasks = []
    for task in seed_tasks():
        if model_cutoff.cutoff > task.as_of:
            logger.warning(
                "skipping %s: model cutoff %s is after task as_of %s", task.task_id, model_cutoff.cutoff, task.as_of
            )
            continue
        admissible_tasks.append(task)

    rows: list[EvalRow] = []
    async with httpx.AsyncClient() as client:
        for task in admissible_tasks:
            answer = await forecast(client, endpoint, task)
            task_score = score(task, answer)
            metrics = " ".join(f"{name}={value:.4f}" for name, value in task_score.metrics.items())
            print(f"{task.task_id:32} {task.question.kind:7} {metrics}")
            rows.append(EvalRow(task=task, answer=answer, task_score=task_score))
    return rows


def report(rows: list[EvalRow], model_id: str, output: Path | None) -> None:
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
    parser.add_argument("--base-url", default=ZAI_BASE_URL)
    parser.add_argument("--api-key-env", default="ZAI_API_KEY", help="Env var holding the API key.")
    parser.add_argument("--output", type=Path, default=None, help="Optional path for JSON results.")
    args = parser.parse_args()
    api_key = os.environ.get(args.api_key_env) or Path("/tmp/zai_key").read_text().strip()
    endpoint = ChatEndpoint(base_url=args.base_url, api_key=api_key, model_id=args.model_id)
    rows = asyncio.run(run_forecasts(endpoint))
    report(rows=rows, model_id=endpoint.model_id, output=args.output)


if __name__ == "__main__":
    main()

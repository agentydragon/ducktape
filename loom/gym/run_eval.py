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
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from finance.evidence.checkout import ensure_checkout
from loom.gym.baseline_llm import LITELLM_BASE_URL, ChatEndpoint, ForecastResult, forecast, forecast_bundle
from loom.gym.dossier import series_dossier
from loom.gym.model_cutoffs import KNOWN_MODEL_CUTOFFS
from loom.gym.monthly_series import MonthlySeries, load_series
from loom.gym.results_store import results_client, upload_run
from loom.gym.scoring import Answer, TaskScore, cluster_bootstrap_ci, score
from loom.gym.series_tasks import all_tasks
from loom.gym.task import Task

logger = logging.getLogger(__name__)

_CONCURRENCY = 4


@dataclass(frozen=True)
class EvalRow:
    task: Task
    answer: Answer
    task_score: TaskScore


@dataclass(frozen=True)
class TokenTotals:
    requests: int
    input_tokens: int
    output_tokens: int


def admissible_tasks(series: list[MonthlySeries], model_id: str, task_filter: str | None, strict: bool) -> list[Task]:
    if model_id not in KNOWN_MODEL_CUTOFFS:
        raise ValueError(f"unknown model — add it to KNOWN_MODEL_CUTOFFS with provenance: {model_id=}")
    model_cutoff = KNOWN_MODEL_CUTOFFS[model_id]
    bound = model_cutoff.weights_released if strict else model_cutoff.knowledge_cutoff
    tasks = []
    for task in all_tasks(series):
        if task_filter is not None and task_filter not in task.task_id:
            continue
        if bound > task.as_of:
            logger.info("skipping %s: model bound %s is after task as_of %s", task.task_id, bound, task.as_of)
            continue
        tasks.append(task)
    return tasks


async def run_forecasts(
    endpoint: ChatEndpoint, series: list[MonthlySeries], tasks: list[Task], with_data: bool, bundled: bool
) -> tuple[list[EvalRow], TokenTotals]:
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    dossiers = {as_of: series_dossier(series, as_of) for as_of in {task.as_of for task in tasks}} if with_data else {}

    groups: list[list[Task]] = []
    if bundled:
        by_bundle: dict[str, list[Task]] = defaultdict(list)
        for task in tasks:
            if task.bundle_id is None:
                groups.append([task])
            else:
                by_bundle[task.bundle_id].append(task)
        groups.extend(by_bundle.values())
    else:
        groups = [[task] for task in tasks]

    async def forecast_group(client: httpx.AsyncClient, group: list[Task]) -> tuple[list[EvalRow], ForecastResult]:
        dossier = dossiers.get(group[0].as_of)
        async with semaphore:
            if len(group) == 1:
                result = await forecast(client, endpoint, group[0], dossier=dossier)
            else:
                result = await forecast_bundle(client, endpoint, group, dossier=dossier)
        tasks_by_id = {task.task_id: task for task in group}
        rows = [
            EvalRow(task=tasks_by_id[task_id], answer=answer, task_score=score(tasks_by_id[task_id], answer))
            for task_id, answer in result.answers.items()
        ]
        return rows, result

    async with httpx.AsyncClient() as client:
        outcomes = await asyncio.gather(*(forecast_group(client, group) for group in groups))
    rows = [row for group_rows, _ in outcomes for row in group_rows]
    totals = TokenTotals(
        requests=len(outcomes),
        input_tokens=sum(result.input_tokens for _, result in outcomes),
        output_tokens=sum(result.output_tokens for _, result in outcomes),
    )
    return rows, totals


def _metric_means(rows: list[EvalRow]) -> dict[str, float]:
    values_by_metric: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for name, value in row.task_score.metrics.items():
            values_by_metric[name].append(value)
    return {name: sum(values) / len(values) for name, values in values_by_metric.items()}


def _print_group_mean(label: str, rows: list[EvalRow]) -> None:
    parts = []
    for name, mean_value in sorted(_metric_means(rows).items()):
        clusters: dict[object, list[float]] = defaultdict(list)
        for row in rows:
            if name in row.task_score.metrics:
                clusters[row.task.as_of].append(row.task_score.metrics[name])
        ci = cluster_bootstrap_ci(tuple(clusters.values()))
        parts.append(f"{name}={mean_value:.4f}" + (f" [{ci[0]:.4f},{ci[1]:.4f}]" if ci is not None else ""))
    print(f"mean[{label}] over {len(rows)} tasks: " + " ".join(parts))


def report(rows: list[EvalRow]) -> None:
    for row in rows:
        metrics = " ".join(f"{name}={value:.4f}" for name, value in row.task_score.metrics.items())
        print(f"{row.task.task_id:36} {row.task.question.kind:7} {metrics}")
    for kind in ("binary", "scalar", "categorical"):
        if kind_rows := [row for row in rows if row.task.question.kind == kind]:
            _print_group_mean(kind, kind_rows)
    # Raw pinball is not comparable across series (S&P points vs CPI index), so
    # also break means out per series prefix; mean_pinball_log is the
    # cross-series-comparable scalar metric.
    for prefix in sorted({row.task.task_id.split("-")[0] for row in rows}):
        _print_group_mean(prefix, [row for row in rows if row.task.task_id.split("-")[0] == prefix])


def run_payload(rows: list[EvalRow], model_id: str, mode: str, totals: TokenTotals) -> dict[str, object]:
    return {
        "model_id": model_id,
        "mode": mode,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "totals": {
            "requests": totals.requests,
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
        },
        "results": [
            {"task_id": row.task.task_id, "answer": row.answer.model_dump(), "metrics": row.task_score.metrics}
            for row in rows
        ],
    }


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
        "--with-data", action="store_true", help="Include the as-of-truncated series dossier in the prompt."
    )
    parser.add_argument("--bundled", action="store_true", help="Elicit tasks sharing a bundle_id in one request each.")
    parser.add_argument(
        "--strict", action="store_true", help="Bound admissibility by weights-release date, not knowledge cutoff."
    )
    parser.add_argument("--output", type=Path, default=None, help="Optional path for JSON results.")
    parser.add_argument("--upload", action="store_true", help="Upload results to s3://loom-gym/runs/ on cluster S3.")
    args = parser.parse_args()
    api_key = os.environ.get(args.api_key_env) or Path("/tmp/litellm_key").read_text().strip()
    endpoint = ChatEndpoint(
        base_url=args.base_url,
        api_key=api_key,
        model_id=args.model_id,
        endpoint_model=args.endpoint_model or f"{args.model_id}-anthropic",
    )
    series = list(load_series(ensure_checkout()))
    tasks = admissible_tasks(series, model_id=args.model_id, task_filter=args.task_filter, strict=args.strict)
    if not tasks:
        raise SystemExit(f"no admissible tasks ({args.model_id=}, {args.strict=}, {args.task_filter=})")
    print(
        f"{len(tasks)} admissible tasks for {args.model_id} "
        f"(strict={args.strict}, with_data={args.with_data}, bundled={args.bundled})"
    )
    rows, totals = asyncio.run(run_forecasts(endpoint, series, tasks, with_data=args.with_data, bundled=args.bundled))
    print(
        f"requests={totals.requests} input_tokens={totals.input_tokens} output_tokens={totals.output_tokens} "
        f"tasks_per_request={len(rows) / totals.requests:.2f}"
    )
    report(rows)
    mode = ("data" if args.with_data else "bare") + ("-bundled" if args.bundled else "")
    payload = run_payload(rows=rows, model_id=endpoint.model_id, mode=mode, totals=totals)
    if args.output is not None:
        args.output.write_text(json.dumps(payload, indent=2))
        print(f"wrote {args.output}")
    if args.upload:
        key = upload_run(results_client(), payload, now=datetime.now(UTC))
        print(f"uploaded s3://loom-gym/{key}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Score a frozen HumanEval+ cohort using EvalPlus 0.3.1's verifiers."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from evalplus.data.humaneval import HUMANEVAL_PLUS_VERSION, get_human_eval_plus, get_human_eval_plus_hash
from evalplus.sanitize import sanitize


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True, help="generation JSONL")
    parser.add_argument("--cohort", type=Path, required=True, help="frozen cohort manifest JSON")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--repeat-base", choices=("0", "1", "auto"), default="auto")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    return args


def load_raw(path: Path, task_ids: list[str], repeats: int, repeat_base: str) -> list[dict[str, object]]:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    repeat_values = {int(record["repeat"]) for record in records}
    if repeat_base == "auto":
        if repeat_values == set(range(repeats)):
            repeat_base = "0"
        elif repeat_values == set(range(1, repeats + 1)):
            repeat_base = "1"
        else:
            raise ValueError(f"cannot determine repeat indexing base from {sorted(repeat_values)}")
    wanted = set(task_ids)
    normalized: dict[tuple[str, int], dict[str, object]] = {}
    for record in records:
        task_id = str(record["task_id"])
        if task_id not in wanted:
            raise ValueError(f"raw output contains task outside frozen cohort: {task_id}")
        repeat = int(record["repeat"])
        # The NPU runner stores repeat indices as 0,1,2; use a stable 1-based
        # presentation in scored artifacts while retaining the raw source file.
        if repeat_base == "0":
            repeat += 1
        if not 1 <= repeat <= repeats:
            raise ValueError(f"invalid repeat index {record['repeat']} for {task_id}")
        key = (task_id, repeat)
        if key in normalized:
            raise ValueError(f"duplicate raw output for {task_id} repeat {repeat}")
        if "response" in record:
            response = str(record["response"])
        elif "completion" in record:
            response = str(record["completion"])
        else:
            raise ValueError(f"raw output has no response/completion field: {key}")
        normalized[key] = {**record, "task_id": task_id, "repeat": repeat, "response": response}

    expected = {(task_id, repeat) for task_id in task_ids for repeat in range(1, repeats + 1)}
    missing = sorted(expected - normalized.keys())
    unexpected = sorted(normalized.keys() - expected)
    if missing or unexpected:
        raise ValueError(f"cohort rows incomplete: missing={missing}, unexpected={unexpected}")
    return [normalized[(task_id, repeat)] for task_id in task_ids for repeat in range(1, repeats + 1)]


def main() -> int:
    args = parse_args()
    cohort = json.loads(args.cohort.read_text())
    if cohort["evalplus_version"] != "0.3.1":
        raise RuntimeError(f"unexpected EvalPlus pin: {cohort['evalplus_version']}")
    if HUMANEVAL_PLUS_VERSION != "v0.1.10":
        raise RuntimeError(f"unexpected HumanEval+ release: {HUMANEVAL_PLUS_VERSION}")

    rows = load_raw(args.raw, cohort["task_ids"], cohort["repeats_per_task"], args.repeat_base)
    problems = get_human_eval_plus(version=HUMANEVAL_PLUS_VERSION)
    dataset_hash = get_human_eval_plus_hash(version=HUMANEVAL_PLUS_VERSION)
    missing_problems = sorted(set(cohort["task_ids"]) - problems.keys())
    if missing_problems:
        raise RuntimeError(f"frozen tasks missing from pinned dataset: {missing_problems}")

    sanitized: list[dict[str, object]] = []
    for row in rows:
        problem = problems[str(row["task_id"])]
        code = sanitize(str(row["response"]), entrypoint=problem["entry_point"])
        sanitized.append(
            {
                **row,
                "solution": code,
                "response_sha256": hashlib.sha256(str(row["response"]).encode()).hexdigest(),
                "solution_sha256": hashlib.sha256(code.encode()).hexdigest(),
            }
        )

    # EvalPlus computes the oracle outputs using its pinned canonical solutions,
    # original tests, and augmented tests. Running against the complete pinned
    # dataset also lets its own content-addressed oracle cache be reused between
    # candidate models; only the frozen 20 tasks below are submitted for scoring.
    evaluate = importlib.import_module("evalplus.evaluate")
    expected_output = evaluate.get_groundtruth(problems, dataset_hash, [])
    completion_ids: Counter[str] = Counter()
    futures = {}
    scored: list[dict[str, object]] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
        for row in sanitized:
            task_id = str(row["task_id"])
            completion_id = completion_ids[task_id]
            completion_ids[task_id] += 1
            future = executor.submit(
                evaluate.check_correctness,
                "humaneval",
                completion_id,
                problems[task_id],
                str(row["solution"]),
                expected_output[task_id],
                False,
                True,
                f"{task_id} repeat {row['repeat']}",
            )
            futures[future] = row

        for future in concurrent.futures.as_completed(futures):
            row = futures[future]
            checked = future.result()
            base_status = checked["base"][0]
            plus_status = checked["plus"][0]
            scored.append(
                {
                    **row,
                    "base_status": base_status,
                    "plus_status": plus_status,
                    "base_and_plus_pass": base_status == evaluate.PASS and plus_status == evaluate.PASS,
                }
            )

    cohort_order = {task_id: index for index, task_id in enumerate(cohort["task_ids"])}
    scored.sort(key=lambda row: (cohort_order[str(row["task_id"])], int(row["repeat"])))
    results: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in scored:
        task_id = str(row["task_id"])
        results[task_id].append(
            {
                "task_id": task_id,
                "repeat": row["repeat"],
                "base_status": row["base_status"],
                "plus_status": row["plus_status"],
            }
        )

    base_correct = [
        sum(sample["base_status"] == evaluate.PASS for sample in results[task_id]) for task_id in cohort["task_ids"]
    ]
    plus_correct = [
        sum(
            sample["base_status"] == evaluate.PASS and sample["plus_status"] == evaluate.PASS
            for sample in results[task_id]
        )
        for task_id in cohort["task_ids"]
    ]
    totals = [cohort["repeats_per_task"]] * len(cohort["task_ids"])
    base_pass_at_1 = float(evaluate.estimate_pass_at_k(totals, base_correct, 1).mean())
    plus_pass_at_1 = float(evaluate.estimate_pass_at_k(totals, plus_correct, 1).mean())
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sanitized-samples.jsonl").write_text(
        "".join(json.dumps({"task_id": row["task_id"], "solution": row["solution"]}) + "\n" for row in scored)
    )
    (output_dir / "sample-scores.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in scored)
    )
    summary = {
        "candidate": args.candidate,
        "created_at": datetime.now(UTC).isoformat(),
        "suite": "EvalPlus HumanEval+ fixed local cohort",
        "evalplus_version": "0.3.1",
        "dataset_release": HUMANEVAL_PLUS_VERSION,
        "dataset_hash_md5": dataset_hash,
        "task_ids": cohort["task_ids"],
        "tasks": len(cohort["task_ids"]),
        "repeats_per_task": cohort["repeats_per_task"],
        "samples": len(scored),
        "scorer": "EvalPlus sanitize.sanitize + evaluate.check_correctness (base and plus)",
        "base_pass_at_1": base_pass_at_1,
        "humaneval_plus_pass_at_1": plus_pass_at_1,
        "base_sample_pass_fraction": sum(base_correct) / len(scored),
        "humaneval_plus_sample_pass_fraction": sum(plus_correct) / len(scored),
        "task_scores": {
            task_id: {"base_passes": base_correct[index], "plus_passes": plus_correct[index], "samples": totals[index]}
            for index, task_id in enumerate(cohort["task_ids"])
        },
        "raw_source": str(args.raw.resolve()),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

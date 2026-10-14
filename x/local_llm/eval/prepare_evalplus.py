#!/usr/bin/env python3
"""Export the frozen EvalPlus HumanEval+ prompt cohort byte-for-byte."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evalplus.data.humaneval import HUMANEVAL_PLUS_VERSION, get_human_eval_plus, get_human_eval_plus_hash

SYSTEM_PROMPT = "You are a helpful assistant good at coding."
INSTRUCTION_PREFIX = (
    "Please provide a self-contained Python script that solves the following problem in a markdown code block:"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()

    cohort = json.loads(args.cohort.read_text())
    problems = get_human_eval_plus(version=HUMANEVAL_PLUS_VERSION)
    task_ids = cohort["task_ids"]
    missing = sorted(set(task_ids) - problems.keys())
    if missing:
        raise RuntimeError(f"frozen tasks missing from EvalPlus data: {missing}")

    records = []
    for task_id in task_ids:
        problem_prompt = problems[task_id]["prompt"].strip() + "\n"
        user_prompt = f"{INSTRUCTION_PREFIX}\n```python\n{problem_prompt.strip()}\n```"
        encoded_prompt = user_prompt.encode("utf-8")
        records.append(
            {
                "task_id": task_id,
                "dataset_version": HUMANEVAL_PLUS_VERSION,
                "entry_point": problems[task_id]["entry_point"],
                "problem_prompt": problem_prompt,
                "prompt": user_prompt,
                "prompt_sha256": hashlib.sha256(encoded_prompt).hexdigest(),
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
            }
        )

    data = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    args.prompts.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.prompts.write_text(data)
    args.metadata.write_text(
        json.dumps(
            {
                "evalplus_version": cohort["evalplus_version"],
                "humaneval_plus_release": HUMANEVAL_PLUS_VERSION,
                "dataset_hash_md5": get_human_eval_plus_hash(version=HUMANEVAL_PLUS_VERSION),
                "task_ids": task_ids,
                "system_prompt": SYSTEM_PROMPT,
                "user_prompt_template": (f"{INSTRUCTION_PREFIX}\\n```python\\n{{problem_prompt.strip()}}\\n```"),
                "prompts_sha256": hashlib.sha256(data.encode("utf-8")).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"exported {len(records)} prompts to {args.prompts}")
    print(f"metadata: {args.metadata}")


if __name__ == "__main__":
    main()

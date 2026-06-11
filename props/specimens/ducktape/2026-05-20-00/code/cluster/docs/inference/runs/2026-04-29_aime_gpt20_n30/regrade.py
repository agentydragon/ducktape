#!/usr/bin/env -S env -u PYTHONPATH uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["inspect-ai", "inspect-evals"]
# ///
"""Re-grade AIME logs with a more permissive answer extractor.

Inspect's `aime_scorer` extracts the substring after `ANSWER:` and does an
exact integer match. gpt-oss:20b habitually wraps its final answer in
`\\boxed{N}` or LaTeX inline math `\\(N\\)` despite the prompt's explicit
"do not use \\boxed" instruction, so the scorer's exact match fails on
responses that are mathematically correct.

This script walks each `eval_logs/<effort>/*.eval` file and re-grades every
sample with a more permissive extractor (handling `\\boxed{N}`,
`\\(N\\)`, `$N$`, plain `ANSWER: N`). Reports both numbers per effort and
prints a markdown table suitable for the run README. The strict number is
still the headline (penalizes format-violation, which is a real model
capability gap); the permissive number is a diagnostic showing how many
"incorrect" responses are actually right but mis-formatted.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Match candidates in priority order: \boxed{N}, "ANSWER: \(N\)" or
# "ANSWER: $N$", "ANSWER: N", inline-math \(N\), and bare integer near end.
EXTRACTORS: list[tuple[str, re.Pattern[str]]] = [
    ("boxed", re.compile(r"\\boxed\{(?:\\text\{[^}]*\}|Answer:\s*|.*?=\s*)?([0-9]{1,4})\}")),
    ("answer-paren", re.compile(r"ANSWER:\s*\\\(\s*([0-9]{1,4})\s*\\\)")),
    ("answer-dollar", re.compile(r"ANSWER:\s*\$\s*([0-9]{1,4})\s*\$")),
    ("answer-plain", re.compile(r"ANSWER:\s*([0-9]{1,4})\b")),
    ("inline-paren", re.compile(r"\\\(\s*([0-9]{1,4})\s*\\\)")),
    ("display-eq", re.compile(r"=\s*([0-9]{1,4})\s*\.\s*$", re.MULTILINE)),
]


def regrade_one(content: str, target: int) -> tuple[bool, str | None, str]:
    """Try each extractor on the response tail; return (correct, answer, which_pattern)."""
    tail = content[-1500:]
    for name, pat in EXTRACTORS:
        for m in pat.finditer(tail):
            try:
                ans = int(m.group(1))
            except (TypeError, ValueError):
                continue
            return ans == target, str(ans), name
    return False, None, "no-match"


def assistant_text(sample: dict[str, Any]) -> str:
    msgs = sample.get("messages") or []
    if not msgs:
        return ""
    last = msgs[-1]
    c = last.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def load_log(eval_path: Path) -> dict[str, Any]:
    raw = subprocess.check_output(["inspect", "log", "dump", str(eval_path)])
    return json.loads(raw)


def regrade_log(eval_path: Path) -> dict[str, Any]:
    log = load_log(eval_path)
    samples = log["samples"]
    out: list[dict[str, Any]] = []
    for s in samples:
        score = s["scores"]["aime_scorer"]
        try:
            target = int(s["target"])
        except (TypeError, ValueError):
            target = -1
        ok_re, ans_re, which = regrade_one(assistant_text(s), target)
        mu = (s.get("model_usage") or {}).get("openai/gpt-oss:20b") or {}
        out.append(
            {
                "id": s["id"],
                "target": target,
                "inspect_value": score["value"],
                "inspect_answer": score["answer"],
                "regrade_correct": ok_re,
                "regrade_answer": ans_re,
                "regrade_pattern": which,
                "input_tokens": mu.get("input_tokens", 0),
                "output_tokens": mu.get("output_tokens", 0),
                "working_time_s": s.get("working_time", 0.0),
                "total_time_s": s.get("total_time", 0.0),
            }
        )
    return {
        "stats": log.get("stats") or {},
        "model_generate_config": (log.get("eval") or {}).get("model_generate_config") or {},
        "samples": out,
    }


def summarize_effort(samples: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(samples)
    inspect_correct = sum(1 for s in samples if s["inspect_value"] == "C")
    regrade_correct = sum(1 for s in samples if s["regrade_correct"])
    out_tokens = [s["output_tokens"] for s in samples]
    working_times = [s["working_time_s"] for s in samples]
    total_times = [s["total_time_s"] for s in samples]
    return {
        "n": n,
        "inspect_pass_at_1": inspect_correct / n if n else None,
        "regrade_pass_at_1": regrade_correct / n if n else None,
        "inspect_correct": inspect_correct,
        "regrade_correct": regrade_correct,
        "scorer_only_failures": regrade_correct - inspect_correct,
        "avg_output_tokens": sum(out_tokens) / n if n else None,
        "median_output_tokens": sorted(out_tokens)[n // 2] if n else None,
        "max_output_tokens": max(out_tokens) if out_tokens else None,
        "sum_output_tokens": sum(out_tokens),
        "avg_working_time_s": sum(working_times) / n if n else None,
        "median_working_time_s": sorted(working_times)[n // 2] if n else None,
        "sum_working_time_s": sum(working_times),
        "sum_total_time_s": sum(total_times),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--logs-root", default="eval_logs")
    args = p.parse_args()

    root = Path(__file__).resolve().parent / args.logs_root
    if not root.exists():
        print(f"ERROR: {root} not found", file=sys.stderr)
        return 1

    per_effort: dict[str, dict[str, Any]] = {}
    for effort_dir in sorted(root.iterdir()):
        if not effort_dir.is_dir():
            continue
        eval_files = sorted(effort_dir.glob("*.eval"))
        if not eval_files:
            print(f"warn: no .eval in {effort_dir}", file=sys.stderr)
            continue
        if len(eval_files) > 1:
            print(f"warn: multiple .eval in {effort_dir}, using {eval_files[-1].name}", file=sys.stderr)
        regraded = regrade_log(eval_files[-1])
        per_effort[effort_dir.name] = {**regraded, "summary": summarize_effort(regraded["samples"])}

    output = {"efforts": per_effort}
    out_path = Path(__file__).resolve().parent / "regrade.json"
    out_path.write_text(json.dumps(output, indent=2) + "\n")

    # Print markdown summary table
    print()
    print(
        "| effort | inspect pass@1 | regrade pass@1 | scorer-only fails | avg out_tok | median out_tok | max out_tok | sum working_s | sum total_s |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for effort, data in per_effort.items():
        s = data["summary"]
        print(
            f"| {effort} | {s['inspect_correct']}/{s['n']} = {s['inspect_pass_at_1']:.2f} "
            f"| {s['regrade_correct']}/{s['n']} = {s['regrade_pass_at_1']:.2f} "
            f"| {s['scorer_only_failures']} "
            f"| {s['avg_output_tokens']:.0f} "
            f"| {s['median_output_tokens']:.0f} "
            f"| {s['max_output_tokens']} "
            f"| {s['sum_working_time_s']:.0f} "
            f"| {s['sum_total_time_s']:.0f} |"
        )
    print()
    print(f"Wrote {out_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

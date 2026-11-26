"""Render split strategy census JSONL artifacts as a repeatable Markdown table."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median


def read_records(inputs: list[str]) -> list[dict]:
    files: set[Path] = set()
    for value in inputs:
        path = Path(value)
        if path.is_dir():
            files.update(path.rglob("split_strategy_census.jsonl"))
        else:
            files.add(path)
    records = []
    for file in sorted(files):
        if not file.is_file():
            continue
        for line_number, line in enumerate(file.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{file}:{line_number}: {error}") from error
            row["report"] = file.name
            records.append(row)
    return records


def display(value: object) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def strategy_for_scope(scope: object) -> str | None:
    if not isinstance(scope, str):
        return None
    if scope.startswith("linked-structural"):
        return "linked-structural"
    if "slice" in scope:
        return "slice"
    return scope


def strategy_for_attempt(row: dict) -> str:
    if row.get("scope") == "linked-programs" and row.get("certificate_strategy"):
        return str(row["certificate_strategy"])
    if strategy := strategy_for_scope(row.get("scope")):
        return strategy
    strategy = row.get("strategy") or "unknown strategy"
    return "linked-structural" if strategy == "structural" else strategy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", help="JSONL files or directories containing test outputs")
    args = parser.parse_args()
    records = read_records(args.reports)
    if not records:
        parser.error("no split_strategy_census.jsonl records found")

    by_input: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        by_input[row["input"]].append(row)

    certified: dict[str, set[str]] = {}
    for input_id, rows in by_input.items():
        strategies = set()
        for row in rows:
            if row.get("status") != "certified":
                continue
            strategy = strategy_for_attempt(row)
            if strategy:
                strategies.add(strategy)
        certified[input_id] = strategies

    strategies = {strategy for accepted in certified.values() for strategy in accepted}
    classes: dict[str, set[str]] = {f"{strategy} only": set() for strategy in sorted(strategies)}
    classes["multiple strategies"] = set()
    classes["no strategy certified"] = set()
    for input_id, strategies in certified.items():
        if len(strategies) == 0:
            classes["no strategy certified"].add(input_id)
        elif len(strategies) > 1:
            classes["multiple strategies"].add(input_id)
        else:
            strategy = next(iter(strategies))
            classes[f"{strategy} only"].add(input_id)

    print("## Strategy coverage\n")
    for name, inputs in classes.items():
        print(f"- {name}: {len(inputs)} distinct input(s)")
        if name.endswith(" only") and inputs:
            accepted_strategy = name.removesuffix(" only")
            reasons: dict[tuple[str, str], set[str]] = defaultdict(set)
            rejected = [
                row
                for row in records
                if row.get("input") in inputs
                and row.get("status") != "certified"
                and strategy_for_attempt(row) != accepted_strategy
            ]
            for row in rejected:
                strategy = strategy_for_attempt(row)
                reason = row.get("reason_code") or row.get("reason") or "uncoded refusal"
                reason = str(reason).replace("\n", " ")
                if len(reason) > 120:
                    reason = reason[:117] + "..."
                reasons[(strategy, reason)].add(row["input"])
            if reasons:
                print(
                    "  - Other strategy refusals: "
                    + ", ".join(
                        f"{strategy}: {reason} ({len(inputs)})"
                        for (strategy, reason), inputs in sorted(reasons.items(), key=lambda item: -len(item[1]))
                    )
                )

    linked_only = classes.get("generated-proof only", set())
    blockers_by_input: dict[str, set[str]] = {}
    for input_id in linked_only:
        blockers = {str(kind) for row in by_input[input_id] for kind in row.get("replay_blockers", []) or []}
        if blockers:
            blockers_by_input[input_id] = blockers
    if blockers_by_input:
        inputs_by_blocker: dict[str, set[str]] = defaultdict(set)
        for input_id, blockers in blockers_by_input.items():
            for blocker in blockers:
                inputs_by_blocker[blocker].add(input_id)
        print("\n## Replay blockers on linked-only inputs\n")
        print(f"- Classified {len(blockers_by_input)} of {len(linked_only)} generated-proof-only inputs.")
        for blocker, inputs in sorted(inputs_by_blocker.items(), key=lambda item: (-len(item[1]), item[0])):
            print(f"- {blocker}: {len(inputs)} input(s)")
        print("\n| Input | Test(s) | Replay blockers |")
        print("| --- | --- | --- |")
        for input_id in sorted(blockers_by_input):
            tests = sorted({str(row.get("test")) for row in by_input[input_id] if row.get("test")})
            print(f"| {input_id} | {', '.join(tests)} | {', '.join(sorted(blockers_by_input[input_id]))} |")

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        strategy = strategy_for_attempt(row)
        by_strategy[strategy].append(row)
    print("\n## Timing\n")
    for strategy, rows in sorted(by_strategy.items()):
        times = sorted(float(row.get("elapsed_ms", 0)) for row in rows)
        p90 = times[min(len(times) - 1, int(0.9 * (len(times) - 1)))]
        certified_count = sum(row.get("status") == "certified" for row in rows)
        print(
            f"- {strategy}: {len(rows)} attempt(s), {certified_count} certified; "
            f"median {median(times):.2f} ms, p90 {p90:.2f} ms"
        )

    print("\n## Attempts\n")
    print("| Input | Name | Case | Strategy | Verdict | Scope | Reason code | Reason | Time (ms) | Test |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- |")
    for row in sorted(
        records,
        key=lambda item: (item.get("input_name", ""), item["input"], item["strategy"], item.get("elapsed_ms", 0)),
    ):
        print(
            "| "
            + " | ".join(
                display(row.get(key))
                for key in (
                    "input",
                    "input_name",
                    "case_name",
                    "strategy",
                    "status",
                    "scope",
                    "reason_code",
                    "reason",
                    "elapsed_ms",
                    "test",
                )
            )
            + " |"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)

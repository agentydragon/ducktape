"""Collect merged PRs by UTC day and classify latest-visible gate state at merge.

The GitHub search API is capped at 1,000 results for a query. The collector
queries each calendar day, cross-checks the search count against `gh pr list`,
and stops if any daily partition reaches the cap or either source is incomplete.
The classifications use current `statusCheckRollup` timestamps, not a complete
historical attempt log.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

SEARCH_LIMIT = 1000
PULL_FIELDS = "number,url,mergedAt,headRefOid,statusCheckRollup"


class GitHub(Protocol):
    def search_count(self, repo: str, day: str) -> int: ...

    def list_merged_prs(self, repo: str, day: str) -> list[dict[str, Any]]: ...


class GhCli:
    def search_count(self, repo: str, day: str) -> int:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                "search/issues",
                "-f",
                f"q=repo:{repo} is:pr is:merged merged:{day}",
                "-f",
                "per_page=1",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(f"GitHub search failed for {day}: {result.stderr.strip()[:500]}")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"GitHub search returned invalid JSON for {day}") from error
        if not isinstance(response, dict) or response.get("incomplete_results") is not False:
            raise RuntimeError(f"GitHub search reports incomplete results for {day}")
        total = response.get("total_count")
        if not isinstance(total, int) or total < 0:
            raise RuntimeError(f"GitHub search returned no valid total_count for {day}")
        return total

    def list_merged_prs(self, repo: str, day: str) -> list[dict[str, Any]]:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "merged",
                "--search",
                f"merged:{day}",
                "--limit",
                str(SEARCH_LIMIT),
                "--json",
                PULL_FIELDS,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(f"GitHub PR listing failed for {day}: {result.stderr.strip()[:500]}")
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"GitHub PR listing returned invalid JSON for {day}") from error
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError(f"GitHub PR listing returned a malformed result for {day}")
        return rows


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a UTC offset: {value}")
    if parsed.year <= 1970:
        raise ValueError(f"timestamp is a sentinel or predates Actions history: {value}")
    return parsed.astimezone(dt.UTC)


def _days(first: dt.datetime, last: dt.datetime) -> list[dt.date]:
    days = []
    current = first.date()
    while current <= last.date():
        days.append(current)
        current += dt.timedelta(days=1)
    return days


def collect_day(github: GitHub, repo: str, day: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    total = github.search_count(repo, day)
    if total >= SEARCH_LIMIT:
        raise ValueError(
            f"merged PR search for {day} reports {total} results; the 1,000-result "
            "search cap prevents a complete daily partition"
        )
    rows = github.list_merged_prs(repo, day)
    if len(rows) != total:
        raise ValueError(f"merged PR listing for {day} returned {len(rows)} rows; search reported {total}")
    identifiers = [row.get("number") for row in rows]
    if any(not isinstance(number, int) for number in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError(f"merged PR listing for {day} has missing or duplicate PR numbers")
    return rows, {"day_utc": day, "reported": total, "returned": len(rows), "complete": True}


def collect_window(
    github: GitHub, repo: str, since: str, until: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    first, last = _parse_time(since), _parse_time(until)
    if first > last:
        raise ValueError("SINCE_UTC must be before or equal to UNTIL_UTC")
    partitions = []
    rows_by_number: dict[int, dict[str, Any]] = {}
    for day in _days(first, last):
        rows, coverage = collect_day(github, repo, day.isoformat())
        partitions.append(coverage)
        for row in rows:
            number = row["number"]
            if number in rows_by_number:
                raise ValueError(f"daily merged-PR partitions overlap on PR #{number}")
            rows_by_number[number] = row

    exact = []
    for row in rows_by_number.values():
        merged_at = _parse_time(row["mergedAt"])
        if first <= merged_at <= last:
            exact.append(row)
    exact.sort(key=lambda row: (_parse_time(row["mergedAt"]), row["number"]))
    return exact, partitions


def _latest_check(checks: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    matches = [check for check in checks if check.get("__typename") == "CheckRun" and check.get("name") == name]
    if not matches:
        return None
    unordered = [check for check in matches if _valid_check_time(check.get("startedAt")) is None]
    if unordered:
        # A queued check can carry a year-1 sentinel instead of a start clock.
        # Preserve the unknown state rather than letting an older green attempt
        # win solely because the current attempt has no usable timestamp.
        return unordered[-1]
    return max(
        matches,
        key=lambda check: (check.get("startedAt") or "", check.get("completedAt") or "", check.get("detailsUrl") or ""),
    )


def _valid_check_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _parse_time(value)
    except ValueError:
        return None


def classify_gate(pr: Mapping[str, Any], gate: str) -> dict[str, Any]:
    merged_at = pr.get("mergedAt")
    try:
        merged = _parse_time(merged_at) if isinstance(merged_at, str) else None
    except ValueError:
        merged = None
    rollup = pr.get("statusCheckRollup")
    checks = rollup if isinstance(rollup, list) else []
    check = _latest_check(checks, gate)
    if check is None:
        return {
            "name": gate,
            "class": "missing_from_current_rollup",
            "at_merge": "unknown",
            "history_uncertainty": "no matching CheckRun is visible in the current statusCheckRollup",
        }

    started_at = check.get("startedAt")
    completed_at = check.get("completedAt")
    status = check.get("status")
    conclusion = check.get("conclusion")
    started = _valid_check_time(started_at)
    completed = _valid_check_time(completed_at)
    common = {
        "name": gate,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
        "completed_at": completed_at,
        "url": check.get("detailsUrl"),
        "history_uncertainty": "current latest-visible attempt only; superseded historical attempts are not reconstructed",
    }
    if merged is None or started is None or (status == "COMPLETED" and completed is None):
        return {**common, "class": "unclassifiable_current_rollup", "at_merge": "unknown"}
    if completed is not None and completed < started:
        return {**common, "class": "unclassifiable_current_rollup", "at_merge": "unknown"}
    if started > merged:
        return {**common, "class": "latest_attempt_started_after_merge", "at_merge": "unknown"}
    if status != "COMPLETED" or completed is None or completed > merged:
        return {**common, "class": "latest_attempt_straddled_merge", "at_merge": "pending_or_prior_attempt_unknown"}
    if conclusion == "SUCCESS":
        return {**common, "class": "visible_green_by_merge", "at_merge": "latest_visible_attempt_green"}
    if conclusion == "CANCELLED":
        return {**common, "class": "visible_cancelled_by_merge", "at_merge": "latest_visible_attempt_cancelled"}
    if conclusion in {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}:
        return {**common, "class": "visible_red_by_merge", "at_merge": "latest_visible_attempt_failed"}
    return {**common, "class": "visible_non_green_by_merge", "at_merge": "latest_visible_attempt_not_green"}


def classify_pr(pr: Mapping[str, Any], gates: list[str]) -> dict[str, Any]:
    check_states = {gate: classify_gate(pr, gate) for gate in gates}
    classes = {check["class"] for check in check_states.values()}
    priority = (
        ("visible_red_by_merge", "visible_red_by_merge"),
        ("visible_cancelled_by_merge", "visible_cancelled_by_merge"),
        ("visible_non_green_by_merge", "visible_non_green_by_merge"),
        ("latest_attempt_straddled_merge", "latest_attempt_straddled_merge"),
        ("latest_attempt_started_after_merge", "latest_attempt_started_after_merge"),
        ("missing_from_current_rollup", "missing_from_current_rollup"),
        ("unclassifiable_current_rollup", "unclassifiable_current_rollup"),
    )
    overall = next(
        (label for class_name, label in priority if class_name in classes), "all_gates_visible_green_by_merge"
    )
    return {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "merged_at": pr.get("mergedAt"),
        "head_sha": pr.get("headRefOid"),
        "overall": overall,
        "gates": check_states,
    }


def analyze(
    rows: Sequence[Mapping[str, Any]],
    repo: str,
    since: str,
    until: str,
    gates: list[str],
    partitions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not gates or len(set(gates)) != len(gates):
        raise ValueError("at least one unique gate name is required")
    classified = [classify_pr(row, gates) for row in rows]
    gate_counts = {gate: dict(Counter(row["gates"][gate]["class"] for row in classified)) for gate in gates}
    overall_counts = Counter(row["overall"] for row in classified)
    candidates = [row for row in classified if row["overall"] != "all_gates_visible_green_by_merge"]
    return {
        "repository": f"https://github.com/{repo}",
        "window_utc": [since, until],
        "method": "complete UTC-day merged-PR partitions; current exact-head CheckRun rollups compared with mergedAt; latest visible matching attempt per named gate",
        "historical_attempts_complete": False,
        "historical_attempt_uncertainty": "statusCheckRollup exposes current latest-visible checks, not complete historical attempts; later reruns can hide the state that was visible at merge",
        "partitions": [dict(partition) for partition in partitions],
        "returned_total": sum(partition["returned"] for partition in partitions),
        "window_merged_pr_count": len(classified),
        "gate_names": gates,
        "pr_level_classes": dict(overall_counts),
        "gate_level_classes": gate_counts,
        "non_green_or_unknown_candidates": candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="OWNER/REPO")
    parser.add_argument("since", help="inclusive RFC3339 lower bound in UTC")
    parser.add_argument("until", help="inclusive RFC3339 upper bound in UTC")
    parser.add_argument("output", type=Path, help="output JSON path")
    parser.add_argument("--gate", action="append", required=True, help="exact required CheckRun name; may repeat")
    args = parser.parse_args()
    try:
        if "/" not in args.repo:
            raise ValueError("repository must be OWNER/REPO")
        rows, partitions = collect_window(GhCli(), args.repo, args.since, args.until)
        result = analyze(rows, args.repo, args.since, args.until, args.gate, partitions)
        args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

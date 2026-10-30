"""Summarize exact-head PR gate feedback and completed runner occupancy.

Feedback uses GitHub Actions job timestamps only. It requires every named gate
to have a visible successful latest attempt for the same exact head and reports
the first observed PR workflow creation as a proxy clock. It does not inspect
BuildBuddy cached durations or claim that GitHub's visible jobs prove
auto-merge eligibility.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def _timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def _sort_run(run: Mapping[str, Any]) -> tuple[dt.datetime, int]:
    created = _timestamp(run.get("created_at")) or dt.datetime.min.replace(tzinfo=dt.UTC)
    identifier = run.get("id")
    return created, identifier if isinstance(identifier, int) else -1


def _head_source(runs: Iterable[Mapping[str, Any]]) -> tuple[str, str | None]:
    repositories = {
        (run.get("repository") or {}).get("full_name")
        for run in runs
        if isinstance(run.get("repository"), dict)
    }
    head_repositories = {
        (run.get("head_repository") or {}).get("full_name")
        for run in runs
        if isinstance(run.get("head_repository"), dict)
    }
    repositories.discard(None)
    head_repositories.discard(None)
    if len(repositories) != 1 or len(head_repositories) != 1:
        return "unknown", None
    base = next(iter(repositories))
    head = next(iter(head_repositories))
    return ("same-repo" if head == base else "fork"), head


def _job_state(job: Mapping[str, Any]) -> str:
    if job.get("status") != "completed":
        return "pending" if job.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"} else "unknown"
    conclusion = job.get("conclusion")
    if conclusion == "success":
        return "success"
    if conclusion in {"failure", "timed_out", "action_required", "startup_failure"}:
        return "failed"
    if conclusion == "cancelled":
        return "canceled"
    if conclusion == "skipped":
        return "skipped"
    return "unknown"


def _combine_states(states: list[str]) -> str:
    for state in ("failed", "canceled", "pending", "unknown", "skipped"):
        if state in states:
            return state
    return "success" if states and all(state == "success" for state in states) else "missing"


def _latest_attempt_jobs(
    run: Mapping[str, Any], jobs_by_run: Mapping[int, list[Mapping[str, Any]]], gate: str
) -> tuple[list[Mapping[str, Any]], bool]:
    run_id = run.get("id")
    jobs = jobs_by_run.get(run_id, []) if isinstance(run_id, int) else []
    matching = [job for job in jobs if job.get("name") == gate]
    latest_attempt = run.get("run_attempt")
    if not isinstance(latest_attempt, int) or latest_attempt < 1:
        attempts: list[int] = [
            attempt for job in matching if isinstance((attempt := job.get("run_attempt")), int)
        ]
        if not attempts:
            return [], False
        latest_attempt = max(attempts)
        return [job for job in matching if job.get("run_attempt") == latest_attempt], False
    return [job for job in matching if job.get("run_attempt") == latest_attempt], True


def _gate_result(
    gate: str,
    runs: list[Mapping[str, Any]],
    jobs_by_run: Mapping[int, list[Mapping[str, Any]]],
    head_start: dt.datetime | None,
) -> dict[str, Any]:
    source_workflows: set[int] = set()
    for run in runs:
        workflow_id, run_id = run.get("workflow_id"), run.get("id")
        if (
            isinstance(workflow_id, int)
            and isinstance(run_id, int)
            and any(job.get("name") == gate for job in jobs_by_run.get(run_id, []))
        ):
            source_workflows.add(workflow_id)
    if not source_workflows:
        return {"name": gate, "state": "missing", "attempt": "unknown", "workflows": []}

    workflow_results: list[dict[str, Any]] = []
    for workflow_id in sorted(source_workflows):
        workflow_runs = [run for run in runs if run.get("workflow_id") == workflow_id]
        run = max(workflow_runs, key=_sort_run)
        latest_jobs, attempt_known = _latest_attempt_jobs(run, jobs_by_run, gate)
        if not latest_jobs:
            workflow_results.append(
                {
                    "workflow_id": workflow_id,
                    "run_id": run.get("id"),
                    "run_attempt": run.get("run_attempt"),
                    "state": "missing",
                    "attempt_known": attempt_known,
                    "jobs": [],
                }
            )
            continue

        states = [_job_state(job) for job in latest_jobs]
        state = _combine_states(states) if attempt_known else "unknown"
        completed = [job.get("completed_at") for job in latest_jobs]
        completed_times = [_timestamp(value) for value in completed]
        complete_at = max((value for value in completed_times if value is not None), default=None)
        started_at = min(
            (value for value in (_timestamp(job.get("started_at")) for job in latest_jobs) if value is not None),
            default=None,
        )
        created_at = min(
            (value for value in (_timestamp(job.get("created_at")) for job in latest_jobs) if value is not None),
            default=None,
        )
        if state == "success" and complete_at is None:
            state = "unknown"
        workflow_results.append(
            {
                "workflow_id": workflow_id,
                "run_id": run.get("id"),
                "run_attempt": run.get("run_attempt"),
                "state": state,
                "attempt_known": attempt_known,
                "created_at": created_at.isoformat().replace("+00:00", "Z") if created_at else None,
                "started_at": started_at.isoformat().replace("+00:00", "Z") if started_at else None,
                "completed_at": complete_at.isoformat().replace("+00:00", "Z") if complete_at else None,
                "elapsed_seconds": (complete_at - created_at).total_seconds() if complete_at and created_at else None,
                "offset_from_first_run_seconds": (
                    (complete_at - head_start).total_seconds() if complete_at and head_start else None
                ),
                "jobs": [
                    {
                        "id": job.get("id"),
                        "name": job.get("name"),
                        "status": job.get("status"),
                        "conclusion": job.get("conclusion"),
                        "html_url": job.get("html_url"),
                    }
                    for job in latest_jobs
                ],
            }
        )

    state = _combine_states([result["state"] for result in workflow_results])
    complete_times = [
        parsed
        for result in workflow_results
        if result["state"] == "success" and (parsed := _timestamp(result.get("completed_at"))) is not None
    ]
    finished = max(complete_times, default=None)
    return {
        "name": gate,
        "state": state,
        "attempt": "known" if all(result["attempt_known"] for result in workflow_results) else "unknown",
        "completed_at": finished.isoformat().replace("+00:00", "Z") if finished else None,
        "offset_from_first_run_seconds": (finished - head_start).total_seconds() if finished and head_start else None,
        "workflows": workflow_results,
    }


def _head_outcome(source: str, states: list[str]) -> str:
    if source == "fork" and any(state in {"missing", "skipped"} for state in states):
        return "fork-unlinked"
    if source == "unknown":
        return "unknown-source"
    if "failed" in states:
        return "failed"
    if "canceled" in states:
        return "canceled"
    if "pending" in states:
        return "pending"
    if any(state != "success" for state in states):
        return "incomplete"
    return "green-proxy"


def analyze_feedback(
    snapshot: Mapping[str, Any], since: str, until: str, gates: list[str]
) -> dict[str, Any]:
    first, last = _timestamp(since), _timestamp(until)
    if first is None or last is None or first > last:
        raise ValueError("window boundaries must be valid ordered RFC3339 timestamps")
    if not gates or len(set(gates)) != len(gates):
        raise ValueError("at least one unique required gate name is required")
    runs = snapshot.get("runs")
    jobs = snapshot.get("jobs")
    if not isinstance(runs, list) or not isinstance(jobs, list):
        raise ValueError("snapshot must contain runs and jobs arrays")

    window_runs = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("event") == "pull_request"
        and (created := _timestamp(run.get("created_at"))) is not None
        and first <= created <= last
        and isinstance(run.get("head_sha"), str)
    ]
    jobs_by_run: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for job in jobs:
        if isinstance(job, dict) and isinstance(job.get("run_id"), int):
            jobs_by_run[job["run_id"]].append(job)

    grouped: dict[tuple[str | None, str], list[Mapping[str, Any]]] = defaultdict(list)
    for run in window_runs:
        source, head_repo = _head_source([run])
        grouped[(head_repo, run["head_sha"])].append(run)

    heads = []
    for (head_repo, sha), head_runs in sorted(grouped.items(), key=lambda item: (item[0][0] or "", item[0][1])):
        source, _ = _head_source(head_runs)
        start_run = min(head_runs, key=_sort_run)
        head_start = _timestamp(start_run.get("created_at"))
        gate_results = [_gate_result(gate, head_runs, jobs_by_run, head_start) for gate in gates]
        states = [gate["state"] for gate in gate_results]
        outcome = _head_outcome(source, states)
        proxy_times = [
            parsed
            for gate in gate_results
            if gate["state"] == "success" and (parsed := _timestamp(gate.get("completed_at"))) is not None
        ]
        proxy_seconds = (
            (max(proxy_times) - head_start).total_seconds()
            if outcome == "green-proxy" and head_start and len(proxy_times) == len(gates)
            else None
        )
        pr_numbers: set[int] = set()
        for head_run in head_runs:
            pull_requests = head_run.get("pull_requests")
            if isinstance(pull_requests, list):
                for pr in pull_requests:
                    if isinstance(pr, dict) and isinstance((number := pr.get("number")), int):
                        pr_numbers.add(number)
        prs = sorted(pr_numbers)
        heads.append(
            {
                "head_repository": head_repo,
                "head_sha": sha,
                "head_source": source,
                "pull_requests": prs,
                "observed_run_count": len(head_runs),
                "first_observed_run_created_at": (
                    head_start.isoformat().replace("+00:00", "Z") if head_start else None
                ),
                "outcome": outcome,
                "feedback_proxy_seconds": proxy_seconds,
                "gates": gate_results,
            }
        )
    return {
        "repository": snapshot.get("repository"),
        "window_utc": [since, until],
        "feedback_clock": "latest visible successful required-gate completion minus first observed PR workflow creation for the same head repository and SHA",
        "gate_clock": "each gate reports its own job-created-to-completed duration and offset from the first observed PR workflow creation",
        "historical_limit": "GitHub's collected latest workflow attempts do not reconstruct every historical required-check state or prove auto-merge eligibility",
        "cached_duration_policy": "BuildBuddy cache fields are not read or used; the measurements below come from GitHub job timestamps",
        "required_gates": gates,
        "outcomes": dict(Counter(head["outcome"] for head in heads)),
        "heads": heads,
    }


def analyze_occupancy(
    snapshot: Mapping[str, Any], since: str, until: str, workflows: set[str], thresholds: list[int]
) -> dict[str, Any]:
    first, last = _timestamp(since), _timestamp(until)
    if first is None or last is None or first >= last:
        raise ValueError("occupancy window must be a valid nonempty RFC3339 interval")
    if any(value < 1 for value in thresholds):
        raise ValueError("runner thresholds must be positive integers")
    runs = {run.get("id"): run for run in snapshot.get("runs", []) if isinstance(run, dict)}
    durations: dict[str, float] = defaultdict(float)
    events: list[tuple[dt.datetime, int]] = []
    counts: Counter[str] = Counter()
    for job in snapshot.get("jobs", []):
        if not isinstance(job, dict):
            counts["malformed_job"] += 1
            continue
        run = runs.get(job.get("run_id"))
        workflow = run.get("path", "unknown") if isinstance(run, dict) else "unknown"
        if workflows and workflow not in workflows:
            continue
        if job.get("status") != "completed":
            counts["unfinished_or_unknown_status"] += 1
            continue
        runner_id = job.get("runner_id")
        if not isinstance(runner_id, int) or runner_id <= 0:
            counts["no_positive_runner_id"] += 1
            continue
        start, end = _timestamp(job.get("started_at")), _timestamp(job.get("completed_at"))
        if start is None or end is None:
            counts["missing_timestamps"] += 1
            continue
        if end <= start:
            counts["invalid_interval"] += 1
            continue
        clipped_start, clipped_end = max(first, start), min(last, end)
        if clipped_end <= clipped_start:
            counts["outside_window"] += 1
            continue
        seconds = (clipped_end - clipped_start).total_seconds()
        durations[workflow] += seconds
        counts["included_jobs"] += 1
        events.extend(((clipped_start, 1), (clipped_end, -1)))

    active = peak = 0
    previous = first
    seconds_by_busy: dict[int, float] = defaultdict(float)
    for when, delta in sorted(events, key=lambda event: (event[0], event[1])):
        seconds_by_busy[active] += (when - previous).total_seconds()
        active += delta
        peak = max(peak, active)
        previous = when
    seconds_by_busy[active] += (last - previous).total_seconds()
    window_seconds = (last - first).total_seconds()
    return {
        "window_utc": [since, until],
        "scope": "jobs with status completed and a positive runner_id; each interval is clipped to the window; unfinished and unassigned jobs are excluded; BuildBuddy cache data is not read",
        "completed_assigned_job_count": counts["included_jobs"],
        "runner_seconds_by_workflow": dict(sorted(durations.items())),
        "runner_hours_by_workflow": {name: seconds / 3600 for name, seconds in sorted(durations.items())},
        "runner_seconds_total": sum(durations.values()),
        "mean_busy_runners": sum(seconds * busy for busy, seconds in seconds_by_busy.items()) / window_seconds,
        "peak_busy_runners": peak,
        "minutes_at_least_threshold": {
            str(threshold): sum(seconds for busy, seconds in seconds_by_busy.items() if busy >= threshold) / 60
            for threshold in thresholds
        },
        "excluded_jobs": dict(sorted((key, value) for key, value in counts.items() if key != "included_jobs")),
    }


def analyze(snapshot: Mapping[str, Any], since: str, until: str, gates: list[str], workflows: set[str], thresholds: list[int]) -> dict[str, Any]:
    return {
        "feedback": analyze_feedback(snapshot, since, until, gates),
        "occupancy": analyze_occupancy(snapshot, since, until, workflows, thresholds),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--since", required=True, help="inclusive RFC3339 lower bound")
    parser.add_argument("--until", required=True, help="RFC3339 upper bound; occupancy ends just before it")
    parser.add_argument("--gate", action="append", required=True, help="exact required job name; may repeat")
    parser.add_argument("--workflow", action="append", default=[], help="limit occupancy to this workflow path; may repeat")
    parser.add_argument("--runner-threshold", action="append", type=int, default=[], help="busy-runner threshold in addition to the default 18 and 20")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        snapshot = json.loads(args.snapshot.read_text())
        result = analyze(
            snapshot,
            args.since,
            args.until,
            args.gate,
            set(args.workflow),
            [18, 20, *args.runner_threshold],
        )
        args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

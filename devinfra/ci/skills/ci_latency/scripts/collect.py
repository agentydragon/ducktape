"""Collect complete GitHub Actions metadata through the authenticated `gh` CLI.

The Actions runs endpoint exposes at most 1,000 results for one query. Dense
windows are split by second until each query can be paginated and counted. Job
queries use `filter=all`, preserving every job attempt. No logs or credentials
are written to the output directory.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

RUN_LIMIT = 1000
EARLIEST_GITHUB_RUN = "2008-01-01T00:00:00Z"
ACTIVE_STATUSES = ("queued", "in_progress", "pending", "waiting", "requested")


class Api(Protocol):
    def request(self, endpoint: str, params: Mapping[str, str], *, paginate: bool) -> Any:
        """Request one GitHub page or all pages through `gh api`."""


class GhApi:
    """Small `gh api` adapter; authentication remains owned by GitHub CLI."""

    def request(self, endpoint: str, params: Mapping[str, str], *, paginate: bool) -> Any:
        command = ["gh", "api", "--method", "GET", endpoint]
        for key, value in params.items():
            command.extend(("-f", f"{key}={value}"))
        if paginate:
            command.extend(("--paginate", "--slurp"))
        last_error = ""
        for attempt in range(3):
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError as error:
                    raise RuntimeError(f"gh api returned invalid JSON for {endpoint}") from error
            last_error = result.stderr.strip()
            transient = any(
                marker in last_error.lower() for marker in ("timeout", "502", "503", "stream error", "unexpected eof")
            )
            if not transient or attempt == 2:
                break
            time.sleep(attempt + 1)
        raise RuntimeError(f"gh api failed for {endpoint}: {last_error[:500]}")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, separators=(",", ":")) + "\n")


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a UTC offset: {value}")
    return parsed.astimezone(dt.UTC)


def _format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unique_by_id(items: Iterable[Mapping[str, Any]], label: str) -> list[dict[str, Any]]:
    unique: dict[int, dict[str, Any]] = {}
    for item in items:
        identifier = item.get("id")
        if not isinstance(identifier, int):
            raise ValueError(f"{label} item is missing an integer id")
        if identifier in unique:
            raise ValueError(f"{label} pagination repeated id {identifier}")
        unique[identifier] = dict(item)
    return list(unique.values())


def _run_pages(api: Api, endpoint: str, params: Mapping[str, str], expected_count: int) -> list[dict[str, Any]]:
    pages = api.request(endpoint, params, paginate=True)
    if not isinstance(pages, list) or not pages:
        if expected_count == 0 and pages == []:
            return []
        raise ValueError(f"{endpoint} pagination returned no page envelope")
    counts = [page.get("total_count") for page in pages if isinstance(page, dict)]
    if len(counts) != len(pages) or any(count != expected_count for count in counts):
        raise ValueError(f"{endpoint} pagination changed its reported total_count")
    runs = [run for page in pages for run in page.get("workflow_runs", [])]
    if len(runs) != expected_count:
        raise ValueError(f"{endpoint} returned {len(runs)} runs; expected {expected_count}")
    return _unique_by_id(runs, "workflow run")


def collect_runs(
    api: Api, repo: str, first: str, last: str, *, filters: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Fetch all runs in an inclusive time window, partitioning at the API cap."""
    start, end = _parse_time(first), _parse_time(last)
    if start > end:
        raise ValueError("start timestamp must be before or equal to end timestamp")
    if start.microsecond or end.microsecond:
        raise ValueError("Actions collection windows must have whole-second boundaries")
    endpoint = f"repos/{repo}/actions/runs"
    common = {"per_page": "100", **(filters or {})}

    def collect_interval(left: dt.datetime, right: dt.datetime) -> list[dict[str, Any]]:
        interval = {**common, "created": f"{_format_time(left)}..{_format_time(right)}"}
        probe = api.request(endpoint, {**interval, "per_page": "1"}, paginate=False)
        total_count = probe.get("total_count") if isinstance(probe, dict) else None
        if not isinstance(total_count, int) or total_count < 0:
            raise ValueError(f"{endpoint} response is missing a valid total_count")
        if total_count < RUN_LIMIT:
            return _run_pages(api, endpoint, interval, total_count)
        if left == right:
            raise ValueError(
                f"{endpoint} still reports {total_count} runs in one second; "
                "GitHub cannot expose a complete result set for this interval"
            )
        middle_epoch = (int(left.timestamp()) + int(right.timestamp())) // 2
        middle = dt.datetime.fromtimestamp(middle_epoch, dt.UTC)
        next_second = middle + dt.timedelta(seconds=1)
        return collect_interval(left, middle) + collect_interval(next_second, right)

    result = collect_interval(start, end)
    return _unique_by_id(result, "workflow run across time partitions")


def _paginated_collection(api: Api, endpoint: str, params: Mapping[str, str], *, rows_key: str) -> list[dict[str, Any]]:
    pages = api.request(endpoint, params, paginate=True)
    if not isinstance(pages, list):
        raise ValueError(f"{endpoint} pagination did not return a page list")
    rows: list[dict[str, Any]] = []
    totals: list[int] = []
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError(f"{endpoint} returned a malformed page")
        total = page.get("total_count")
        if not isinstance(total, int) or total < 0:
            raise ValueError(f"{endpoint} page is missing total_count")
        totals.append(total)
        values = page.get(rows_key)
        if not isinstance(values, list):
            raise ValueError(f"{endpoint} page is missing {rows_key}")
        rows.extend(value for value in values if isinstance(value, dict))
    if not totals or len(set(totals)) != 1 or len(rows) != totals[0]:
        expected = totals[0] if totals else "a reported total"
        raise ValueError(f"{endpoint} returned {len(rows)} rows; expected {expected}")
    return _unique_by_id(rows, rows_key)


def combine_runs(
    window_runs: Iterable[Mapping[str, Any]], active_runs: Mapping[str, Iterable[Mapping[str, Any]]]
) -> list[dict[str, Any]]:
    """Merge old unfinished runs into the window, preferring their active state."""
    runs = {run["id"]: dict(run) for run in window_runs}
    for status_runs in active_runs.values():
        for run in status_runs:
            runs[run["id"]] = dict(run)
    return [runs[identifier] for identifier in sorted(runs)]


def _plain_array_pages(api: Api, endpoint: str, params: Mapping[str, str]) -> list[list[Any]]:
    pages = api.request(endpoint, params, paginate=True)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError(f"{endpoint} returned malformed array pages")
    return pages


def collect(repo: str, since: str, until: str, out: Path, workflow_ids: set[int]) -> None:
    if out.exists():
        raise ValueError(f"output directory already exists: {out}")
    if not repo or "/" not in repo:
        raise ValueError("repository must be OWNER/REPO")
    start, end = _parse_time(since), _parse_time(until)
    if start > end:
        raise ValueError("SINCE_UTC must be before or equal to UNTIL_UTC")
    if start.microsecond or end.microsecond:
        raise ValueError("collection boundaries must use whole-second precision")
    out.mkdir(parents=True)
    (out / "jobs").mkdir()
    (out / "checks").mkdir()
    (out / "collection_started_at.txt").write_text(_format_time(dt.datetime.now(dt.UTC)) + "\n")
    (out / "source_commit.txt").write_text(
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() + "\n"
    )
    (out / "repository.txt").write_text(repo + "\n")
    (out / "window.txt").write_text(f"{since}\n{until}\n")
    api = GhApi()
    window_runs = collect_runs(api, repo, since, until)
    _write_json(out / "runs.json", window_runs)

    now = _format_time(dt.datetime.now(dt.UTC))
    active: dict[str, list[dict[str, Any]]] = {}
    for status in ACTIVE_STATUSES:
        active[status] = collect_runs(api, repo, EARLIEST_GITHUB_RUN, now, filters={"status": status})
        _write_json(out / f"active-{status}.json", active[status])
    runs = combine_runs(window_runs, active)
    _write_json(out / "all_runs.json", runs)

    selected = [run for run in runs if not workflow_ids or run.get("workflow_id") in workflow_ids]

    def fetch_jobs(run: Mapping[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        run_id = run["id"]
        jobs = _paginated_collection(
            api, f"repos/{repo}/actions/runs/{run_id}/jobs", {"filter": "all", "per_page": "100"}, rows_key="jobs"
        )
        _write_json(out / "jobs" / f"{run_id}.json", [{"jobs": jobs}])
        return run_id, jobs

    jobs_by_run: dict[int, list[dict[str, Any]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs_by_run = dict(pool.map(fetch_jobs, selected))

    pull_pages = _plain_array_pages(api, f"repos/{repo}/pulls", {"state": "open", "per_page": "100"})
    _write_json(out / "pulls.json", pull_pages)
    pull_requests = [pr for page in pull_pages for pr in page]

    def fetch_checks(pr: Mapping[str, Any]) -> None:
        number, sha = pr["number"], pr["head"]["sha"]
        pages = _paginated_collection(
            api,
            f"repos/{repo}/commits/{sha}/check-runs",
            {"filter": "latest", "per_page": "100"},
            rows_key="check_runs",
        )
        # Keep the historic page envelope expected by checks.jq.
        _write_json(out / "checks" / f"{number}.json", [{"total_count": len(pages), "check_runs": pages}])

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch_checks, pull_requests))

    all_jobs = _unique_by_id((job for jobs in jobs_by_run.values() for job in jobs), "job attempt")
    finished = _format_time(dt.datetime.now(dt.UTC))
    (out / "collection_finished_at.txt").write_text(finished + "\n")
    snapshot = {
        "repository": repo,
        "collected_at": finished,
        "window": {"since": since, "until": until},
        "runs": runs,
        "jobs": all_jobs,
    }
    _write_json(out / "snapshot.json", snapshot)
    (out / "collection.json").write_text(
        json.dumps(
            {
                "window_runs": len(window_runs),
                "all_runs": len(runs),
                "selected_runs": len(selected),
                "job_attempts": len(all_jobs),
                "active_run_counts": {status: len(items) for status, items in active.items()},
                "pagination": "verified against each endpoint total_count",
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Snapshot: {out / 'snapshot.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="OWNER/REPO")
    parser.add_argument("since", help="inclusive RFC3339 lower bound in UTC")
    parser.add_argument("until", help="inclusive RFC3339 upper bound in UTC")
    parser.add_argument("output", type=Path, help="new output directory")
    parser.add_argument(
        "--workflow-id",
        action="append",
        type=int,
        default=[],
        help="limit job and open-PR check details to runs for this workflow; may repeat",
    )
    args = parser.parse_args()
    try:
        collect(args.repo, args.since, args.until, args.output, set(args.workflow_id))
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as error:
        print(f"collect: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

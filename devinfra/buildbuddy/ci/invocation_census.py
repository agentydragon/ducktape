"""Census deterministic BuildBuddy invocation IDs for GitHub Actions jobs."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import json
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.buildbuddy.cli import CLIError, run as bbapi

_METADATA_FIELDS = (
    "durationUsec",
    "createdAtUsec",
    "updatedAtUsec",
    "commitSha",
    "bazelExitCode",
    "success",
    "invocationStatus",
    "repoUrl",
    "branchName",
    "role",
)


@dataclasses.dataclass(frozen=True)
class InvocationRequest:
    invocation_id: str
    run: dict[str, Any]
    attempt: str
    role: str
    jobs: tuple[dict[str, Any], ...]


@dataclasses.dataclass(frozen=True)
class StatusResponse:
    metadata: tuple[dict[str, Any], ...]


@dataclasses.dataclass(frozen=True)
class QueryFailure:
    kind: str


QueryResult = StatusResponse | QueryFailure
StatusQuery = Callable[[str], QueryResult]


def parse_timestamp(value: str, *, option: str) -> dt.datetime:
    try:
        timestamp = dt.datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{option} must be an RFC3339 timestamp") from error
    if timestamp.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{option} must include a timezone")
    return timestamp.astimezone(dt.UTC)


def parse_positive_int(value: str, *, option: str, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{option} must be an integer") from error
    if not 1 <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"{option} must be between 1 and {maximum}")
    return parsed


def split_values(value: str, *, option: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in value.split(","))
    if not values or any(not part for part in values):
        raise argparse.ArgumentTypeError(f"{option} must be a comma-separated list of non-empty values")
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError(f"{option} must not contain duplicates")
    return values


def _run_attempt(run: dict[str, Any], job: dict[str, Any]) -> str:
    attempt = job.get("run_attempt", run.get("run_attempt"))
    if isinstance(attempt, bool) or not isinstance(attempt, (int, str)) or not str(attempt).isdigit():
        raise ValueError(f"job {job.get('id')} has no valid run_attempt")
    if int(attempt) < 1:
        raise ValueError(f"job {job.get('id')} has no valid run_attempt")
    return str(attempt)


def _run_id(run: dict[str, Any]) -> str:
    value = run.get("id")
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        raise ValueError("snapshot run has no valid numeric id")
    if int(value) < 1:
        raise ValueError("snapshot run has no valid numeric id")
    return str(value)


def _created_at(run: dict[str, Any]) -> dt.datetime:
    value = run.get("created_at")
    if not isinstance(value, str):
        raise ValueError(f"run {_run_id(run)} has no created_at timestamp")
    try:
        return parse_timestamp(value, option="snapshot run created_at")
    except argparse.ArgumentTypeError as error:
        raise ValueError(f"run {_run_id(run)} has an invalid created_at timestamp") from error


def select_invocations(
    snapshot: dict[str, Any],
    *,
    job_name: str,
    roles: Sequence[str],
    events: Sequence[str] = (),
    workflow_path: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> list[InvocationRequest]:
    if not job_name:
        raise ValueError("job_name must not be empty")
    if not roles or any(not role for role in roles) or len(set(roles)) != len(roles):
        raise ValueError("roles must be a non-empty sequence of unique names")
    runs = snapshot.get("runs")
    jobs = snapshot.get("jobs")
    if not isinstance(runs, list) or not isinstance(jobs, list):
        raise ValueError("snapshot must contain runs and jobs arrays")

    runs_by_id: dict[str, dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("snapshot runs must contain objects")
        run_id = _run_id(run)
        if run_id in runs_by_id:
            raise ValueError(f"snapshot contains duplicate run id {run_id}")
        runs_by_id[run_id] = run

    selected: dict[str, InvocationRequest] = {}
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("snapshot jobs must contain objects")
        if job.get("name") != job_name:
            continue
        run_id_value = job.get("run_id")
        if isinstance(run_id_value, bool) or not isinstance(run_id_value, (int, str)):
            raise ValueError(f"selected job {job.get('id')} has no run_id")
        run_id = str(run_id_value)
        run = runs_by_id.get(run_id)
        if run is None:
            raise ValueError(f"selected job refers to absent run {run_id}")
        if events and run.get("event") not in events:
            continue
        if workflow_path is not None and run.get("path") != workflow_path:
            continue
        if since is not None or until is not None:
            created_at = _created_at(run)
            if since is not None and created_at < since:
                continue
            if until is not None and created_at >= until:
                continue

        attempt = _run_attempt(run, job)
        for role in roles:
            identifier = str(invocation_id(run_id=run_id, attempt=attempt, role=role))
            existing = selected.get(identifier)
            if existing is None:
                selected[identifier] = InvocationRequest(
                    invocation_id=identifier, run=run, attempt=attempt, role=role, jobs=(job,)
                )
            else:
                selected[identifier] = dataclasses.replace(existing, jobs=(*existing.jobs, job))
    return sorted(selected.values(), key=lambda item: (int(item.run["id"]), int(item.attempt), item.role))


def _allowlisted_metadata(values: Any, *, invocation: str | None = None) -> tuple[dict[str, Any], ...]:
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise ValueError("invocation metadata must be an array of objects")
    if invocation is not None and any(
        value.get("invocationId") is not None and value["invocationId"] != invocation for value in values
    ):
        raise ValueError("response item invocationId does not match requested ID")
    return tuple({key: value[key] for key in _METADATA_FIELDS if key in value} for value in values)


def parse_status_response(payload: Any, *, invocation: str | None = None) -> StatusResponse:
    if not isinstance(payload, dict):
        raise ValueError("response must be a JSON object")
    payload_id = payload.get("invocation_id")
    if invocation is not None and payload_id is not None and payload_id != invocation:
        raise ValueError("saved response invocation_id does not match requested ID")
    if payload == {}:
        metadata = []
    elif "invocation" in payload:
        metadata = payload["invocation"]
    else:
        # Older report audit files stored the filtered response under `metadata`.
        if "metadata" not in payload:
            raise ValueError("response has no invocation metadata")
        metadata = payload["metadata"]
    return StatusResponse(_allowlisted_metadata(metadata, invocation=invocation))


def query_live(invocation: str, *, timeout_seconds: int) -> QueryResult:
    try:
        raw = bbapi("--json", "invocation", "status", invocation, timeout=timeout_seconds)
    except CLIError as error:
        cause = error.__cause__
        if isinstance(cause, subprocess.TimeoutExpired):
            kind = "timeout"
        elif isinstance(cause, subprocess.CalledProcessError):
            kind = "bbapi_failed"
        elif isinstance(cause, OSError):
            kind = "bbapi_unavailable"
        else:
            kind = "bbapi_error"
        return QueryFailure(kind)
    try:
        payload = json.loads(raw)
        return parse_status_response(payload, invocation=invocation)
    except UnicodeDecodeError, ValueError:
        return QueryFailure("invalid_response")


def query_offline(invocation: str, *, response_dir: Path) -> QueryResult:
    path = response_dir / f"{invocation}.json"
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return QueryFailure("offline_response_missing")
    except OSError, UnicodeDecodeError:
        return QueryFailure("offline_response_unreadable")
    except ValueError:
        return QueryFailure("invalid_response")
    try:
        return parse_status_response(payload, invocation=invocation)
    except ValueError:
        return QueryFailure("invalid_response")


def state_for(result: QueryResult) -> str:
    if isinstance(result, QueryFailure):
        return "query_error"
    statuses = {str(item.get("invocationStatus", "UNKNOWN_INVOCATION_STATUS")) for item in result.metadata}
    if not statuses:
        return "missing"
    if len(statuses) > 1:
        return "mixed"
    status = next(iter(statuses))
    return {
        "COMPLETE_INVOCATION_STATUS": "complete",
        "DISCONNECTED_INVOCATION_STATUS": "disconnected",
        "PARTIAL_INVOCATION_STATUS": "in_progress",
    }.get(status, "unknown")


def _action_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: job[key]
        for key in (
            "id",
            "name",
            "status",
            "conclusion",
            "created_at",
            "started_at",
            "completed_at",
            "runner_id",
            "head_sha",
        )
        if key in job
    }


def make_record(
    request: InvocationRequest, result: QueryResult, *, query_mode: str, observed_at: str
) -> dict[str, Any]:
    metadata = result.metadata if isinstance(result, StatusResponse) else ()
    return {
        "invocation_id": request.invocation_id,
        "role": request.role,
        "attempt": request.attempt,
        "state": state_for(result),
        "query_mode": query_mode,
        "observed_at": observed_at,
        "query_error": result.kind if isinstance(result, QueryFailure) else None,
        "actions": {
            "run_id": _run_id(request.run),
            "event": request.run.get("event"),
            "workflow_path": request.run.get("path"),
            "run_created_at": request.run.get("created_at"),
            "run_conclusion": request.run.get("conclusion"),
            "actions_run_head_sha": request.run.get("head_sha"),
            "jobs": [_action_job(job) for job in request.jobs],
        },
        "buildbuddy_commit_shas": sorted({str(item["commitSha"]) for item in metadata if item.get("commitSha")}),
        "metadata": list(metadata),
    }


def collect_statuses(
    requests: Sequence[InvocationRequest],
    *,
    query: StatusQuery,
    max_workers: int,
    query_mode: str,
    observed_at: str,
    progress_every: int = 0,
) -> list[dict[str, Any]]:
    results: dict[str, QueryResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_request = {executor.submit(query, request.invocation_id): request for request in requests}
        for completed_count, future in enumerate(concurrent.futures.as_completed(future_to_request), start=1):
            request = future_to_request[future]
            # query_live/query_offline convert expected per-ID failures to QueryFailure;
            # unexpected programming errors should remain visible.
            results[request.invocation_id] = future.result()
            if progress_every and completed_count % progress_every == 0:
                print(f"queried {completed_count}/{len(requests)} invocation IDs", file=sys.stderr, flush=True)
    return [
        make_record(request, results[request.invocation_id], query_mode=query_mode, observed_at=observed_at)
        for request in requests
    ]


def make_report(
    *,
    snapshot: dict[str, Any],
    requests: Sequence[InvocationRequest],
    records: Sequence[dict[str, Any]],
    selection: dict[str, Any],
    query_mode: str,
    observed_at: str,
) -> dict[str, Any]:
    counts = Counter(record["state"] for record in records)
    return {
        "schema_version": 1,
        "query_mode": query_mode,
        "observed_at": observed_at,
        "snapshot_collected_at": snapshot.get("collected_at"),
        "selection": selection,
        "summary": {"selected_invocations": len(requests), "states": dict(sorted(counts.items()))},
        "invocations": list(records),
    }


def write_report(report: dict[str, Any], output: Path | None) -> None:
    if output is None:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as temporary:
        json.dump(report, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(output)


def _load_snapshot(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("snapshot must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot", type=Path, required=True, help="Actions snapshot JSON containing runs and jobs arrays"
    )
    parser.add_argument("--job-name", required=True, help="Exact GitHub Actions job name to census")
    parser.add_argument("--roles", required=True, help="Explicit comma-separated BuildBuddy invocation roles")
    parser.add_argument("--events", help="Optional comma-separated GitHub Actions events to include")
    parser.add_argument("--workflow-path", help="Optional exact Actions workflow path filter")
    parser.add_argument("--since", help="Optional inclusive run-created lower bound (RFC3339)")
    parser.add_argument("--until", help="Optional exclusive run-created upper bound (RFC3339)")
    parser.add_argument(
        "--max-workers", type=lambda value: parse_positive_int(value, option="--max-workers", maximum=32), default=4
    )
    parser.add_argument(
        "--timeout-seconds",
        type=lambda value: parse_positive_int(value, option="--timeout-seconds", maximum=300),
        default=30,
    )
    parser.add_argument(
        "--offline-responses-dir",
        type=Path,
        help="Replay saved <invocation-id>.json status responses instead of calling bbapi",
    )
    parser.add_argument(
        "--progress-every", type=lambda value: parse_positive_int(value, option="--progress-every", maximum=1000000)
    )
    parser.add_argument("--output", type=Path, help="Write JSON to this path atomically instead of stdout")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        roles = split_values(args.roles, option="--roles")
        events = split_values(args.events, option="--events") if args.events else ()
        since = parse_timestamp(args.since, option="--since") if args.since else None
        until = parse_timestamp(args.until, option="--until") if args.until else None
        if since is not None and until is not None and since >= until:
            raise argparse.ArgumentTypeError("--since must be before --until")
        snapshot = _load_snapshot(args.snapshot)
        requests = select_invocations(
            snapshot,
            job_name=args.job_name,
            roles=roles,
            events=events,
            workflow_path=args.workflow_path,
            since=since,
            until=until,
        )
    except (argparse.ArgumentTypeError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))

    observed_at = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    if args.offline_responses_dir is None:
        query_mode = "live"

        def query(identifier: str) -> QueryResult:
            return query_live(identifier, timeout_seconds=args.timeout_seconds)

    else:
        query_mode = "offline_replay"
        response_dir = args.offline_responses_dir

        def query(identifier: str) -> QueryResult:
            return query_offline(identifier, response_dir=response_dir)

    records = collect_statuses(
        requests,
        query=query,
        max_workers=args.max_workers,
        query_mode=query_mode,
        observed_at=observed_at,
        progress_every=args.progress_every or 0,
    )
    report = make_report(
        snapshot=snapshot,
        requests=requests,
        records=records,
        selection={
            "job_name": args.job_name,
            "roles": list(roles),
            "events": list(events),
            "workflow_path": args.workflow_path,
            "since": args.since,
            "until": args.until,
        },
        query_mode=query_mode,
        observed_at=observed_at,
    )
    write_report(report, args.output)
    if report["summary"]["states"].get("query_error", 0):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

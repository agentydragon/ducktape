#!/usr/bin/env python3
"""Inspect Forgejo Actions from the command line.

Two subcommands:

- `timing` — per-job CI duration distribution from `/api/v1/repos/.../actions/tasks`
  (answers "why is CI slow?"). Timing-field gotchas this encodes, verified live:
  there is **no** `conclusion` and **no** `stopped_at` (`status` carries
  `success`/`failure`/`cancelled`/`running`, `updated_at` is completion time); the start
  field is **`run_started_at`, not `started_at`** (reading `started_at` silently yields
  `None` and drops every row); duration is **run + queue** wall time (the runner is
  capacity-limited), so outliers are filtered by `--max-seconds`; and the endpoint
  ignores `limit`, returning the whole list under `workflow_runs` (slice client-side).
- `logs` — a run's step logs, driven through the web UI endpoints (answers "why did this
  run fail?"). Discovers the current UI endpoint shape from the run page instead of
  hardcoding REST-like IDs, since this deployment has no REST log-download route.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import statistics
import sys
import urllib.parse
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import httpx

# ── timing ───────────────────────────────────────────────────────────────────

FINISHED = {"success", "failure"}


@dataclass(frozen=True)
class Task:
    id: int
    name: str
    status: str
    run_started_at: str | None
    updated_at: str | None

    @classmethod
    def from_row(cls, row: dict) -> Task:
        # run_started_at, NOT started_at — the latter does not exist on this endpoint.
        return cls(
            id=row.get("id", 0),
            name=row.get("name", "?"),
            status=row.get("status", "?"),
            run_started_at=row.get("run_started_at"),
            updated_at=row.get("updated_at"),
        )

    @property
    def duration_seconds(self) -> float | None:
        """Run+queue wall time, or None if unfinished / timestamps missing."""
        if self.status not in FINISHED or not self.run_started_at or not self.updated_at:
            return None
        secs = (
            dt.datetime.fromisoformat(self.updated_at) - dt.datetime.fromisoformat(self.run_started_at)
        ).total_seconds()
        return secs if secs >= 0 else None


@dataclass(frozen=True)
class JobStats:
    name: str
    n: int
    p_min: float
    p50: float
    p90: float
    p_max: float


def recent_finished(rows: list[dict], limit: int) -> list[Task]:
    """The `limit` most-recent tasks (by id) that carry a usable duration.

    Filter for a usable duration first, then take the newest `limit`, so an
    unfinished/stuck row in the window doesn't shrink the analyzed sample.
    """
    tasks = sorted((Task.from_row(r) for r in rows), key=lambda t: t.id, reverse=True)
    return [t for t in tasks if t.duration_seconds is not None][:limit]


def summarize(tasks: list[Task], max_seconds: float) -> tuple[list[JobStats], int]:
    """Per-job duration stats, plus the count dropped as over-`max_seconds` outliers."""
    kept: dict[str, list[float]] = defaultdict(list)
    dropped = 0
    for t in tasks:
        d = t.duration_seconds
        assert d is not None  # recent_finished() already filtered
        if d > max_seconds:
            dropped += 1
            continue
        kept[t.name].append(d)
    stats = []
    for name, xs in sorted(kept.items()):
        xs.sort()
        stats.append(
            JobStats(
                name=name,
                n=len(xs),
                p_min=xs[0],
                p50=statistics.median(xs),
                p90=xs[min(int(len(xs) * 0.9), len(xs) - 1)],
                p_max=xs[-1],
            )
        )
    return stats, dropped


def fetch_tasks(forgejo_url: str, owner: str, repo: str) -> list[dict]:
    url = f"{forgejo_url.rstrip('/')}/api/v1/repos/{owner}/{repo}/actions/tasks"
    user, password = os.environ.get("FORGEJO_USER"), os.environ.get("FORGEJO_PASSWORD")
    # Explicit Basic auth if given; otherwise trust_env lets httpx use ~/.netrc.
    auth = (user, password) if user and password else None
    with httpx.Client(trust_env=True, timeout=30.0) as client:
        resp = client.get(url, auth=auth, headers={"Accept": "application/json"})
        resp.raise_for_status()
    # resp.json() is Any (untyped external boundary); list() pins it to a concrete type.
    return list(resp.json().get("workflow_runs", []))


def _configure_timing(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--forgejo-url", default=os.environ.get("FORGEJO_URL", "https://git.allegedly.works"))
    parser.add_argument("--limit", type=int, default=200, help="recent tasks to analyze")
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=1800.0,
        help="drop durations above this as outliers (default = runner job timeout)",
    )
    parser.add_argument("--list", action="store_true", help="also list individual recent tasks")
    parser.set_defaults(func=_run_timing)


def _run_timing(args: argparse.Namespace) -> int:
    tasks = recent_finished(fetch_tasks(args.forgejo_url, args.owner, args.repo), args.limit)
    if not tasks:
        print("no finished tasks with usable timestamps found", file=sys.stderr)
        return 1

    if args.list:
        print(f"{'id':>7} {'job':<14} {'status':<9} {'dur':>7}")
        for t in tasks:
            print(f"{t.id:>7} {t.name:<14} {t.status:<9} {t.duration_seconds:>6.0f}s")
        print()

    stats, dropped = summarize(tasks, args.max_seconds)
    print(f"per-job duration (run+queue wall time; {len(tasks)} finished tasks analyzed)")
    print(f"{'job':<14} {'n':>4} {'min':>6} {'p50':>6} {'p90':>6} {'max':>6}")
    for s in stats:
        print(f"{s.name:<14} {s.n:>4} {s.p_min:>5.0f}s {s.p50:>5.0f}s {s.p90:>5.0f}s {s.p_max:>5.0f}s")
    if dropped:
        print(f"\n{dropped} task(s) dropped as > {args.max_seconds:.0f}s outliers (queue wait / stuck rows)")
    return 0


# ── logs ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunPageState:
    actions_url: str
    run_index: str
    job_index: str
    attempt: str
    initial_post_response: dict[str, Any]

    def log_endpoint(self, forgejo_url: str) -> str:
        base = forgejo_url.rstrip("/")
        actions = self.actions_url if self.actions_url.startswith("/") else "/" + self.actions_url
        return f"{base}{actions}/runs/{self.run_index}/jobs/{self.job_index}/attempt/{self.attempt}"


@dataclass(frozen=True)
class LogLine:
    timestamp: str | None
    message: str


class _ForgejoHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csrf: str | None = None
        self.run_attrs: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name: value or "" for name, value in attrs}
        if attr_map.get("name") == "_csrf" and "value" in attr_map:
            self.csrf = attr_map["value"]
        if "data-actions-url" in attr_map:
            self.run_attrs = attr_map


def parse_csrf(login_html: str) -> str:
    parser = _ForgejoHtmlParser()
    parser.feed(login_html)
    return parser.csrf or ""


def parse_run_page(run_html: str) -> RunPageState:
    parser = _ForgejoHtmlParser()
    parser.feed(run_html)
    attrs = parser.run_attrs
    if attrs is None:
        raise ValueError("run page did not contain a data-actions-url element")

    required = {
        "data-actions-url": "actions URL",
        "data-run-index": "run index",
        "data-job-index": "job index",
        "data-attempt-number": "attempt number",
        "data-initial-post-response": "initial post response",
    }
    missing = [label for key, label in required.items() if not attrs.get(key)]
    if missing:
        raise ValueError("run page is missing " + ", ".join(missing))

    initial_raw = html.unescape(attrs["data-initial-post-response"])
    try:
        initial = json.loads(initial_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("data-initial-post-response was not JSON") from exc

    if not isinstance(initial, dict):
        raise ValueError("data-initial-post-response JSON was not an object")

    return RunPageState(
        actions_url=attrs["data-actions-url"],
        run_index=attrs["data-run-index"],
        job_index=attrs["data-job-index"],
        attempt=attrs["data-attempt-number"],
        initial_post_response=initial,
    )


def _looks_like_steps(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    return all(isinstance(item, dict) for item in value) and any(
        any(key in item for key in ("index", "name", "status", "conclusion")) for item in value
    )


def _walk_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield child
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield child
            yield from _walk_values(child)


def extract_steps(initial_post_response: dict[str, Any]) -> list[dict[str, Any]]:
    state = initial_post_response.get("state")
    if isinstance(state, dict):
        current_job = state.get("currentJob")
        if isinstance(current_job, dict) and _looks_like_steps(current_job.get("steps")):
            return list(current_job["steps"])
    for value in _walk_values(initial_post_response):
        if _looks_like_steps(value):
            return list(value)
    return []


def build_log_payload(step: int) -> dict[str, Any]:
    return {"logCursors": [{"step": step, "cursor": None, "expanded": True}]}


def iter_log_lines(response: dict[str, Any]) -> Iterable[LogLine]:
    logs = response.get("logs")
    if not isinstance(logs, dict):
        return
    steps_log = logs.get("stepsLog")
    if not isinstance(steps_log, list):
        return
    for step_log in steps_log:
        if not isinstance(step_log, dict):
            continue
        lines = step_log.get("lines")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if isinstance(line, str):
                yield LogLine(timestamp=None, message=line)
            elif isinstance(line, dict):
                message = line.get("message", line.get("log", ""))
                yield LogLine(timestamp=line.get("timestamp") or line.get("time"), message=str(message))


def parse_log_response(response_text: str) -> list[LogLine]:
    parsed = json.loads(response_text)
    if not isinstance(parsed, dict):
        raise ValueError("log response JSON was not an object")
    return list(iter_log_lines(parsed))


def _response_text(response: httpx.Response) -> str:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400]
        raise RuntimeError(f"HTTP {exc.response.status_code} from {exc.request.url}: {body}") from exc
    return response.text


def login(forgejo_url: str, username: str, password: str, timeout: float) -> httpx.Client:
    login_url = f"{forgejo_url.rstrip('/')}/user/login"
    client = httpx.Client(follow_redirects=True, timeout=timeout)
    try:
        login_page = _response_text(client.get(login_url))
        _response_text(
            client.post(login_url, data={"_csrf": parse_csrf(login_page), "user_name": username, "password": password})
        )
    except Exception:
        client.close()
        raise
    return client


def fetch_run_page(client: httpx.Client, forgejo_url: str, owner: str, repo: str, run_number: str) -> str:
    path = "/".join(
        urllib.parse.quote(part.strip("/"), safe="") for part in (owner, repo, "actions", "runs", run_number)
    )
    return _response_text(client.get(f"{forgejo_url.rstrip('/')}/{path}"))


def fetch_step_logs(client: httpx.Client, forgejo_url: str, state: RunPageState, step: int) -> list[LogLine]:
    return parse_log_response(
        _response_text(client.post(state.log_endpoint(forgejo_url), json=build_log_payload(step)))
    )


def _step_index(position: int, step: dict[str, Any]) -> str:
    for key in ("index", "number", "step"):
        if key in step:
            return str(step[key])
    return str(position)


def _step_name(step: dict[str, Any]) -> str:
    for key in ("name", "title", "displayName", "summary"):
        if key in step:
            return str(step[key])
    return ""


def _step_status(step: dict[str, Any]) -> str:
    for key in ("status", "conclusion", "state"):
        if key in step:
            return str(step[key])
    return ""


def _print_steps(steps: list[dict[str, Any]]) -> None:
    for position, step in enumerate(steps):
        print("\t".join([_step_index(position, step), _step_status(step), _step_name(step)]))


def _print_logs(lines: list[LogLine], timestamps: bool) -> None:
    for line in lines:
        if timestamps:
            print("\t".join([line.timestamp or "", line.message]))
        else:
            print(line.message)


def _configure_logs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--forgejo-url", default=os.environ.get("FORGEJO_URL"), help="Forgejo base URL")
    parser.add_argument("--owner", required=True, help="Repository owner")
    parser.add_argument("--repo", required=True, help="Repository name")
    parser.add_argument("--run", dest="run_number", required=True, help="UI run number, not REST run id")
    parser.add_argument("--user", default=os.environ.get("FORGEJO_USER"), help="Forgejo username")
    parser.add_argument(
        "--password",
        default=os.environ.get("FORGEJO_PASSWORD") or os.environ.get("FORGEJO_PASS"),
        help="Forgejo password; prefer FORGEJO_PASSWORD or FORGEJO_PASS over shell history",
    )
    parser.add_argument("--step", action="append", type=int, help="Step index to expand; repeatable")
    parser.add_argument("--list-steps", action="store_true", help="List step indexes from the run page")
    parser.add_argument("--timestamps", action="store_true", help="Print timestamp<TAB>message")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="Per-request timeout in seconds; applies to each attempted address"
    )
    parser.set_defaults(func=_run_logs)


def _run_logs(args: argparse.Namespace) -> int:
    if not args.forgejo_url:
        raise SystemExit("--forgejo-url or FORGEJO_URL is required")
    if not args.user:
        raise SystemExit("--user or FORGEJO_USER is required")
    if not args.password:
        raise SystemExit("--password, FORGEJO_PASSWORD, or FORGEJO_PASS is required")

    client = login(args.forgejo_url, args.user, args.password, args.timeout)
    try:
        state = parse_run_page(fetch_run_page(client, args.forgejo_url, args.owner, args.repo, args.run_number))

        if args.list_steps or not args.step:
            _print_steps(extract_steps(state.initial_post_response))
            if not args.step:
                return 0

        for step in args.step:
            _print_logs(fetch_step_logs(client, args.forgejo_url, state, step), args.timestamps)
        return 0
    finally:
        client.close()


# ── dispatch ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forgejo", description="Inspect Forgejo Actions.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _configure_timing(subparsers.add_parser("timing", help="per-job CI duration distribution"))
    _configure_logs(subparsers.add_parser("logs", help="fetch a run's step logs from the web UI"))
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())

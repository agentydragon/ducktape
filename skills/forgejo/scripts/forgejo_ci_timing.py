#!/usr/bin/env python3
"""Summarize Forgejo Actions task durations to diagnose slow CI.

The `/api/v1/repos/{owner}/{repo}/actions/tasks` endpoint returns job rows, but
its timing fields are quirky enough that a naive reading is wrong (see the
per-field gotchas below). This helper reads them correctly and prints a per-job
duration distribution, which is what "why is CI slow?" actually needs — as
opposed to `fetch_forgejo_logs.py`, which is for "why did this run fail?".

Gotchas this encodes, verified against the live deployment (haku/haku-state):

- There is **no** `conclusion` field and **no** `stopped_at`. `status` carries
  `success` / `failure` / `cancelled` / `running`; `updated_at` is the last-touch
  time, i.e. completion time for a finished task.
- The start field is **`run_started_at`, not `started_at`** (there is no
  `started_at`). Reading `started_at` silently yields `None` and drops every row.
- Duration = `updated_at - run_started_at`, meaningful only for a **finished**
  task (`status in {success, failure}`). It is **run + queue** wall time: the
  runner is capacity-limited, so a row can sit queued before it runs. Outliers
  (stuck/cancelled rows, or long queue waits) are filtered by `--max-seconds`
  (default 1800, the runner's own job timeout) and the drop count is reported —
  never silently truncated.
- The endpoint ignores `limit` on this deployment and returns the whole task
  list under `workflow_runs`; slice client-side (`--limit`, applied after
  sorting by `id` descending).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass

import httpx

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
        start = dt.datetime.fromisoformat(self.run_started_at)
        end = dt.datetime.fromisoformat(self.updated_at)
        secs = (end - start).total_seconds()
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
    return resp.json().get("workflow_runs", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args()

    rows = fetch_tasks(args.forgejo_url, args.owner, args.repo)
    tasks = recent_finished(rows, args.limit)
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


if __name__ == "__main__":
    raise SystemExit(main())

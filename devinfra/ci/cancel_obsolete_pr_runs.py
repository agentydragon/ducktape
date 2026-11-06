"""Retire obsolete ordinary PR validation runs as replacements start or their PR closes.

Stdlib only: executed from the controller workflow's trusted default-branch SHA.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from devinfra.ci.pr_mergeability import GitHubApi

WORKFLOW_PATH = ".github/workflows/ci.yml"
NIX_WHEEL_CHECK_WORKFLOW_PATH = ".github/workflows/nix-wheel-check.yml"
CLOSED_PR_WORKFLOW_PATHS = (WORKFLOW_PATH, NIX_WHEEL_CHECK_WORKFLOW_PATH)
ACTIVE_STATUSES = frozenset({"requested", "waiting", "pending", "queued", "in_progress"})


def positive(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"Expected a positive integer, received {value!r}")
    return value


def sha(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Expected a full lowercase commit SHA")
    return value


@dataclass(frozen=True)
class Association:
    number: int
    id: int
    base_repository_id: int
    head_repository_id: int


@dataclass(frozen=True)
class ClosedPull:
    association: Association
    head_branch: str
    head_sha: str

    @classmethod
    def parse(cls, data: dict[str, Any]) -> ClosedPull:
        head_branch = data["head"]["ref"]
        if not isinstance(head_branch, str) or not head_branch:
            raise ValueError("Expected a non-empty pull request head branch")
        return cls(
            association=Association(
                number=positive(data["number"]),
                id=positive(data["id"]),
                base_repository_id=positive(data["base"]["repo"]["id"]),
                head_repository_id=positive(data["head"]["repo"]["id"]),
            ),
            head_branch=head_branch,
            head_sha=sha(data["head"]["sha"]),
        )


@dataclass(frozen=True)
class Run:
    id: int
    number: int
    attempt: int
    workflow_id: int
    repository_id: int
    repository_name: str
    head_repository_id: int
    head_sha: str
    head_branch: str
    event: str
    path: str
    status: str
    pull: Association | None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Run:
        pulls = data["pull_requests"]
        pull = None
        if len(pulls) == 1:
            item = pulls[0]
            pull = Association(
                number=positive(item["number"]),
                id=positive(item["id"]),
                base_repository_id=positive(item["base"]["repo"]["id"]),
                head_repository_id=positive(item["head"]["repo"]["id"]),
            )
        return cls(
            id=positive(data["id"]),
            number=positive(data["run_number"]),
            attempt=positive(data["run_attempt"]),
            workflow_id=positive(data["workflow_id"]),
            repository_id=positive(data["repository"]["id"]),
            repository_name=data["repository"]["full_name"],
            head_repository_id=positive(data["head_repository"]["id"]),
            head_sha=sha(data["head_sha"]),
            head_branch=data["head_branch"],
            event=data["event"],
            path=data["path"],
            status=data["status"],
            pull=pull,
        )

    def same_repository_pr_validation(
        self, *, repository_id: int, repository: str, workflow_id: int, workflow_paths: tuple[str, ...]
    ) -> bool:
        return (
            self.repository_id == repository_id
            and self.repository_name == repository
            and self.head_repository_id == repository_id
            and self.workflow_id == workflow_id
            and self.path in workflow_paths
            and self.event == "pull_request"
            and self.attempt == 1
            and self.pull is not None
            and self.pull.base_repository_id == repository_id
            and self.pull.head_repository_id == repository_id
            and bool(self.head_branch)
        )

    def ordinary_pr(self, *, repository_id: int, repository: str, workflow_id: int) -> bool:
        return self.same_repository_pr_validation(
            repository_id=repository_id, repository=repository, workflow_id=workflow_id, workflow_paths=(WORKFLOW_PATH,)
        )

    def closed_pr_validation(self, *, repository_id: int, repository: str, workflow_id: int) -> bool:
        return self.same_repository_pr_validation(
            repository_id=repository_id,
            repository=repository,
            workflow_id=workflow_id,
            workflow_paths=CLOSED_PR_WORKFLOW_PATHS,
        )


def obsolete(candidate: Run, trigger: Run) -> bool:
    return (
        candidate.ordinary_pr(
            repository_id=trigger.repository_id, repository=trigger.repository_name, workflow_id=trigger.workflow_id
        )
        and candidate.pull == trigger.pull
        and candidate.head_branch == trigger.head_branch
        and candidate.id != trigger.id
        and candidate.number < trigger.number
        and candidate.head_sha != trigger.head_sha
        and candidate.status in ACTIVE_STATUSES
    )


class CancellationApi(GitHubApi):
    def cancel(self, run_id: int) -> None:
        # Exactly one request. A transport failure may follow an accepted write;
        # never retry it, force-cancel, or wait for the target's terminal state.
        request = Request(
            f"{self.api_url}/repos/{self.repository}/actions/runs/{run_id}/cancel",
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "ducktape-obsolete-pr-runs",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urlopen(request, timeout=30) as response:
            if response.status != 202:
                raise RuntimeError(f"Unexpected cancellation response: HTTP {response.status}")
        # HTTP errors, including 409 Conflict, propagate and stop processing.
        # Conflict alone does not establish that the run has become terminal.


def relevant_head(api: CancellationApi, trigger: Run) -> bool:
    assert trigger.pull is not None
    pull = api.get(f"pulls/{trigger.pull.number}")
    return relevant_open_head(pull, trigger)


def relevant_open_head(pull: dict[str, Any], trigger: Run) -> bool:
    assert trigger.pull is not None
    return (
        positive(pull["id"]) == trigger.pull.id
        and positive(pull["number"]) == trigger.pull.number
        and pull["state"] == "open"
        and positive(pull["base"]["repo"]["id"]) == trigger.repository_id
        and positive(pull["head"]["repo"]["id"]) == trigger.repository_id
        and pull["head"]["ref"] == trigger.head_branch
        and sha(pull["head"]["sha"]) == trigger.head_sha
    )


def relevant_closed_pull(api: CancellationApi, number: int, expected: ClosedPull) -> bool:
    pull = api.get(f"pulls/{number}")
    return pull["state"] == "closed" and ClosedPull.parse(pull) == expected


def cancel_obsolete(api: CancellationApi, event: dict[str, Any]) -> None:
    if event["action"] != "requested":
        return
    repository_id = positive(event["repository"]["id"])
    if event["repository"]["full_name"] != api.repository:
        return
    observed = Run.parse(event["workflow_run"])
    if not observed.closed_pr_validation(
        repository_id=repository_id, repository=api.repository, workflow_id=observed.workflow_id
    ):
        return
    trigger = Run.parse(api.get(f"actions/runs/{observed.id}"))
    # Event identity is the immutable cutoff. A modified API identity is not a
    # replacement cutoff, including an observed manual rerun of the trigger.
    # Status may advance between event delivery and GET; other fields may not.
    if trigger != replace(observed, status=trigger.status):
        return
    workflow = api.get(f"actions/workflows/{trigger.workflow_id}")
    if positive(workflow["id"]) != trigger.workflow_id or workflow["path"] != trigger.path:
        return
    assert trigger.pull is not None
    pull = api.get(f"pulls/{trigger.pull.number}")
    if pull["state"] == "closed":
        closed = ClosedPull.parse(pull)
        if closed.association != trigger.pull or closed.head_branch != trigger.head_branch:
            return
        # A requested run can arrive after the close-event sweep. Its own
        # trusted workflow_run event repeats the closed-PR cleanup so queued
        # work created later is retired too.
        _cancel_closed_pr_runs(api, repository_id, api.repository, closed, workflow_paths=(trigger.path,))
        return
    # Keep the open-PR admission optimization scoped to ordinary Bazel CI.
    # The Nix wheel check has its own concurrency group and is only included
    # here as a late-run signal for a PR that has already closed.
    if not trigger.ordinary_pr(repository_id=repository_id, repository=api.repository, workflow_id=trigger.workflow_id):
        return
    if not relevant_open_head(pull, trigger):
        return
    page = 1
    seen: set[int] = set()
    while True:
        query = urlencode({"event": "pull_request", "branch": trigger.head_branch, "per_page": 100, "page": page})
        result = api.get(f"actions/workflows/{trigger.workflow_id}/runs?{query}")
        runs = result["workflow_runs"]
        for data in runs:
            candidate = Run.parse(data)
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            if not obsolete(candidate, trigger):
                continue
            fresh = Run.parse(api.get(f"actions/runs/{candidate.id}"))
            if fresh != replace(candidate, status=fresh.status):
                continue
            if not obsolete(fresh, trigger):
                continue
            # Stop a delayed controller once a newer head becomes current. This
            # narrows the unavoidable head-read -> cancel-POST race (no API CAS).
            if not relevant_head(api, trigger):
                return
            print(f"Requesting cancellation of obsolete run {fresh.id} for PR #{trigger.pull.number}")
            api.cancel(fresh.id)
        if len(runs) < 100:
            return
        page += 1


def cancel_closed_pr_runs(api: CancellationApi, event: dict[str, Any]) -> None:
    """Cancel active first-attempt validation runs for one closed same-repository PR."""
    if event["action"] != "closed":
        return
    repository_id = positive(event["repository"]["id"])
    if event["repository"]["full_name"] != api.repository:
        return
    closed = ClosedPull.parse(event["pull_request"])
    if closed.association.base_repository_id != repository_id or closed.association.head_repository_id != repository_id:
        return
    _cancel_closed_pr_runs(api, repository_id, api.repository, closed)


def _cancel_closed_pr_runs(
    api: CancellationApi,
    repository_id: int,
    repository: str,
    closed: ClosedPull,
    *,
    workflow_paths: tuple[str, ...] = CLOSED_PR_WORKFLOW_PATHS,
) -> None:
    number = closed.association.number
    if not relevant_closed_pull(api, number, closed):
        return

    for workflow_path in workflow_paths:
        workflow = api.get(f"actions/workflows/{Path(workflow_path).name}")
        workflow_id = positive(workflow["id"])
        if workflow["path"] != workflow_path:
            continue

        page = 1
        seen: set[int] = set()
        while True:
            query = urlencode({"event": "pull_request", "branch": closed.head_branch, "per_page": 100, "page": page})
            result = api.get(f"actions/workflows/{workflow_id}/runs?{query}")
            runs = result["workflow_runs"]
            for data in runs:
                candidate = Run.parse(data)
                if candidate.id in seen:
                    continue
                seen.add(candidate.id)
                if not _closed_pr_candidate(candidate, closed, repository_id, repository, workflow_id, workflow_path):
                    continue
                fresh = Run.parse(api.get(f"actions/runs/{candidate.id}"))
                if fresh != replace(candidate, status=fresh.status):
                    continue
                if not _closed_pr_candidate(fresh, closed, repository_id, repository, workflow_id, workflow_path):
                    continue
                # A PR may be reopened or its head changed while this controller is
                # paging. Recheck the closure identity immediately before each write.
                if not relevant_closed_pull(api, number, closed):
                    return
                print(f"Requesting cancellation of active validation run {fresh.id} for closed PR #{number}")
                api.cancel(fresh.id)
            if len(runs) < 100:
                break
            page += 1


def _closed_pr_candidate(
    run: Run, closed: ClosedPull, repository_id: int, repository: str, workflow_id: int, workflow_path: str
) -> bool:
    return (
        run.path == workflow_path
        and run.closed_pr_validation(repository_id=repository_id, repository=repository, workflow_id=workflow_id)
        and run.pull == closed.association
        and run.head_branch == closed.head_branch
        and run.status in ACTIVE_STATUSES
    )


def main() -> None:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    if event_name not in {"workflow_run", "pull_request_target"}:
        raise ValueError(f"Unsupported GITHUB_EVENT_NAME: {event_name}")
    api = CancellationApi(repository=os.environ["GITHUB_REPOSITORY"], token=os.environ["GITHUB_TOKEN"])
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if event_name == "workflow_run":
        cancel_obsolete(api, event)
    else:
        cancel_closed_pr_runs(api, event)


if __name__ == "__main__":
    main()

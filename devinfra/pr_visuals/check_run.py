"""Create or update the check run that tracks PR visual publication."""

from __future__ import annotations

import argparse
import os
import re
from typing import Any, Literal

from github import Auth, Github
from pydantic import BaseModel

CheckConclusion = Literal[
    "action_required", "cancelled", "failure", "neutral", "skipped", "stale", "startup_failure", "success", "timed_out"
]
CheckStatus = Literal["completed", "in_progress"]
_VISUAL_REVIEW_EXTERNAL_ID = re.compile(r"pr-visual-review:(\d+):(\d+)")
CHECK_CONCLUSIONS = (
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "stale",
    "startup_failure",
    "success",
    "timed_out",
)


class CheckRunOutput(BaseModel):
    title: str
    summary: str

    def api_payload(self) -> dict[str, str | list[dict[str, str | int]]]:
        return {"title": self.title, "summary": self.summary}


def _superseded_check_name(name: str, run_id: int, attempt: int) -> str:
    return f"{name} (CI run {run_id} attempt {attempt})"


def _duplicate_check_name(name: str, run_id: int, attempt: int, check_run_id: int) -> str:
    return f"{name} (duplicate CI run {run_id} attempt {attempt}, check {check_run_id})"


def _is_duplicate_check(check: Any) -> bool:
    return " (duplicate CI run " in check.name


def _is_superseded_attempt(*, repo: Any, commit_sha: str, external_id: str) -> bool:
    if not external_id.startswith("pr-visual-review:"):
        return False
    match = _VISUAL_REVIEW_EXTERNAL_ID.fullmatch(external_id)
    if match is None:
        raise ValueError(f"invalid visual review external ID {external_id!r}")

    run_id, attempt = (int(value) for value in match.groups())
    workflow_run = repo.get_workflow_run(run_id)
    if workflow_run.head_sha != commit_sha:
        raise ValueError(f"workflow run {run_id} has head {workflow_run.head_sha}, expected {commit_sha}")

    candidates = [
        (run.run_number, run.run_attempt)
        for run in repo.get_workflow_runs(head_sha=commit_sha)
        if run.head_sha == commit_sha and run.workflow_id == workflow_run.workflow_id
    ]
    if not candidates:
        raise ValueError(f"no workflow runs found for visual review head {commit_sha}")
    return max(candidates) > (workflow_run.run_number, attempt)


def _canonical_check(existing: list[Any]) -> Any:
    # A previous publisher can have created a duplicate with the same external ID.
    # Prefer an existing canonical-name check, then a completed one, and finally the
    # highest check-run ID as a stable choice among duplicates.
    canonical_candidates = [check for check in existing if not _is_duplicate_check(check)] or existing
    completed = [check for check in canonical_candidates if check.status == "completed"]
    return max(completed or canonical_candidates, key=lambda check: check.id)


def _retire_duplicate_checks(
    *, existing: list[Any], canonical: Any, name: str, run_id: int, attempt: int, canonical_name: str
) -> None:
    for duplicate in existing:
        if duplicate is canonical:
            continue
        duplicate_name = _duplicate_check_name(name, run_id, attempt, duplicate.id)
        if duplicate.status == "in_progress":
            duplicate.edit(
                name=duplicate_name,
                status="completed",
                conclusion="cancelled",
                output=CheckRunOutput(
                    title=duplicate_name, summary=f"Duplicate check run. The canonical check is {canonical_name}."
                ).api_payload(),
            )
        elif duplicate.name != duplicate_name:
            # Keep the duplicate's terminal conclusion; only remove it from the
            # same-name ordering used by GitHub's default check-run filter.
            duplicate.edit(name=duplicate_name)


def _rename_if_superseded(
    *,
    repo: Any,
    commit_sha: str,
    external_id: str | None,
    attempt_identity: tuple[int, int] | None,
    name: str,
    check: Any,
) -> None:
    if external_id is None or attempt_identity is None:
        return
    if _is_superseded_attempt(repo=repo, commit_sha=commit_sha, external_id=external_id):
        check.edit(name=_superseded_check_name(name, *attempt_identity))


def upsert_check_run(
    *,
    repository: str,
    commit_sha: str,
    status: CheckStatus,
    summary: str,
    token: str,
    conclusion: CheckConclusion | None = None,
    details_url: str | None = None,
    external_id: str | None = None,
    name: str = "PR visual review",
) -> None:
    if (status == "completed") != (conclusion is not None):
        raise ValueError("completed checks require a conclusion; in-progress checks must not have one")

    with Github(auth=Auth.Token(token)) as github:
        repo = github.get_repo(repository)
        attempt_identity: tuple[int, int] | None = None
        is_superseded = False
        if external_id is not None and external_id.startswith("pr-visual-review:"):
            match = _VISUAL_REVIEW_EXTERNAL_ID.fullmatch(external_id)
            if match is None:
                raise ValueError(f"invalid visual review external ID {external_id!r}")
            run_id, attempt = (int(value) for value in match.groups())
            attempt_identity = (run_id, attempt)
            is_superseded = _is_superseded_attempt(repo=repo, commit_sha=commit_sha, external_id=external_id)
        check_name = (
            _superseded_check_name(name, *attempt_identity) if is_superseded and attempt_identity is not None else name
        )

        existing = []
        if external_id is not None:
            existing = [
                check
                for check in repo.get_commit(commit_sha).get_check_runs(filter="all")
                if check.external_id == external_id
            ]
        if len(existing) > 1 and attempt_identity is None:
            raise ValueError(f"multiple {name!r} checks have external ID {external_id!r}")
        canonical = _canonical_check(existing) if existing else None
        if len(existing) > 1 and attempt_identity is not None and canonical is not None:
            _retire_duplicate_checks(
                existing=existing,
                canonical=canonical,
                name=name,
                run_id=attempt_identity[0],
                attempt=attempt_identity[1],
                canonical_name=check_name,
            )
        if status == "in_progress" and canonical is not None and canonical.status == "completed":
            if canonical.name != check_name:
                canonical.edit(name=check_name)
            if not is_superseded:
                _rename_if_superseded(
                    repo=repo,
                    commit_sha=commit_sha,
                    external_id=external_id,
                    attempt_identity=attempt_identity,
                    name=name,
                    check=canonical,
                )
            return

        output = CheckRunOutput(title=check_name, summary=summary[:65000]).api_payload()
        optional: dict[str, Any] = {
            **({"conclusion": conclusion} if conclusion is not None else {}),
            **({"details_url": details_url} if details_url is not None else {}),
            **({"external_id": external_id} if external_id is not None else {}),
        }
        if canonical is not None:
            canonical.edit(name=check_name, status=status, output=output, **optional)
        else:
            canonical = repo.create_check_run(
                name=check_name, head_sha=commit_sha, status=status, output=output, **optional
            )

        # A newer retry can start while this update is in flight. Recheck after the
        # write so a late older attempt keeps its real result without becoming the
        # latest same-name check run.
        if not is_superseded:
            _rename_if_superseded(
                repo=repo,
                commit_sha=commit_sha,
                external_id=external_id,
                attempt_identity=attempt_identity,
                name=name,
                check=canonical,
            )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--repository", required=True)
    result.add_argument("--sha", required=True)
    result.add_argument("--status", choices=("completed", "in_progress"), required=True)
    result.add_argument("--conclusion", choices=CHECK_CONCLUSIONS)
    result.add_argument("--summary", required=True)
    result.add_argument("--details-url")
    result.add_argument("--external-id")
    result.add_argument("--name", default="PR visual review")
    return result


def main() -> None:
    args = parser().parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN is required to publish a check run")
    upsert_check_run(
        repository=args.repository,
        commit_sha=args.sha,
        status=args.status,
        conclusion=args.conclusion,
        summary=args.summary,
        details_url=args.details_url,
        external_id=args.external_id,
        name=args.name,
        token=token,
    )


if __name__ == "__main__":
    main()

"""Create or update the check run that tracks PR visual publication."""

from __future__ import annotations

import argparse
import os
from typing import Any, Literal

from github import Auth, Github

CheckConclusion = Literal[
    "action_required", "cancelled", "failure", "neutral", "skipped", "stale", "startup_failure", "success", "timed_out"
]
CheckStatus = Literal["completed", "in_progress"]
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
        existing = []
        if external_id is not None:
            existing = [
                check
                for check in repo.get_commit(commit_sha).get_check_runs(check_name=name)
                if check.external_id == external_id
            ]
        if len(existing) > 1:
            raise ValueError(f"multiple {name!r} checks have external ID {external_id!r}")

        output = {"title": name, "summary": summary[:65000]}
        optional: dict[str, Any] = {
            **({"conclusion": conclusion} if conclusion is not None else {}),
            **({"details_url": details_url} if details_url is not None else {}),
            **({"external_id": external_id} if external_id is not None else {}),
        }
        if existing:
            existing[0].edit(name=name, status=status, output=output, **optional)
        else:
            repo.create_check_run(name=name, head_sha=commit_sha, status=status, output=output, **optional)


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

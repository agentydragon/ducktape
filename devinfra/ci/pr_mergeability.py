"""Resolve and verify the merge commit a fork PR's CI is built from.

Stdlib only: the workflows run this with the runner's bare ``python3``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# GitHub normally builds a merge commit within seconds; the tail bounds a stuck computation to ~5 minutes.
RETRY_DELAYS = (1, 2, 4, 8, *(15,) * 18)


class GitHubApiError(RuntimeError):
    pass


class SourceRevisionError(RuntimeError):
    pass


class MergeCommitUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class Resolved:
    merge_sha: str


@dataclass(frozen=True)
class Conflicted:
    detail: str


@dataclass(frozen=True)
class Obsolete:
    detail: str


@dataclass(frozen=True)
class Pending:
    problem: str


class GitHubApi:
    def __init__(self, *, repository: str, token: str, api_url: str | None = None) -> None:
        self.repository = repository
        self.token = token
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def get(self, path: str) -> Any:
        request = Request(
            f"{self.api_url}/repos/{self.repository}/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "ducktape-pr-mergeability",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise GitHubApiError(f"GitHub API returned HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise GitHubApiError(f"Could not reach the GitHub API: {error.reason}") from error
        except TimeoutError as error:
            raise GitHubApiError(f"GitHub API request timed out: {error}") from error

    def get_pull_request(self, number: int) -> dict[str, Any]:
        return cast(dict[str, Any], self.get(f"pulls/{number}"))

    def get_git_commit(self, sha: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.get(f"git/commits/{sha}"))


def _inspect(api: GitHubApi, *, number: int, head_sha: str) -> Resolved | Conflicted | Obsolete | Pending:
    try:
        pull = api.get_pull_request(number)
    except GitHubApiError as error:
        return Pending(f"GitHub could not provide PR #{number}: {error}")
    if pull["state"] != "open":
        return Obsolete(f"PR #{number} is {pull['state']}.")
    if pull["head"]["sha"] != head_sha:
        return Obsolete(
            f"PR #{number} head is now `{pull['head']['sha']}`, not `{head_sha}`; the run for that head covers it."
        )
    mergeable = pull.get("mergeable")
    if mergeable is False:
        return Conflicted(
            f"PR #{number} cannot be cleanly merged with `{pull['base']['ref']}` "
            f"(merge state `{pull.get('mergeable_state', 'unknown')}`), so GitHub builds no merge commit for it. "
            "Resolve the conflict and push. No Bazel or Nix work was started."
        )
    if mergeable is None:
        return Pending("GitHub has not finished calculating whether the PR is mergeable.")
    merge_sha = pull.get("merge_commit_sha")
    if not merge_sha:
        return Pending("GitHub reports the PR as mergeable but has not supplied its merge commit SHA.")
    try:
        parents = [parent["sha"] for parent in api.get_git_commit(merge_sha)["parents"]]
    except GitHubApiError as error:
        return Pending(f"GitHub's merge commit `{merge_sha}` is not available: {error}")
    # The first parent is whichever base commit GitHub merged onto, routinely behind the live tip, exactly as for a
    # pull_request run. Only the PR head must match.
    if len(parents) == 2 and parents[1] == head_sha:
        return Resolved(merge_sha)
    return Pending(
        f"GitHub's merge commit `{merge_sha}` has parents `{', '.join(parents) or 'none'}`; "
        f"expected PR head `{head_sha}` as the second parent."
    )


def resolve_merge_commit(
    api: GitHubApi,
    *,
    number: int,
    head_sha: str,
    retry_delays: Sequence[float] = RETRY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> Resolved | Conflicted | Obsolete:
    """Wait for GitHub to build the merge commit of ``head_sha``. Polling the PR is what makes GitHub compute it."""
    problem = ""
    for delay in (*retry_delays, None):
        outcome = _inspect(api, number=number, head_sha=head_sha)
        if not isinstance(outcome, Pending):
            return outcome
        problem = outcome.problem
        if delay is not None:
            sleep(delay)
    raise MergeCommitUnavailableError(
        f"GitHub did not build a merge commit for PR #{number} head `{head_sha}` within {sum(retry_delays):g}s: "
        f"{problem} This is a GitHub merge-source failure, not a Bazel or Nix result. "
        "Re-run this job; if it persists, push a new commit."
    )


def verify_source_revision(
    resolved_sha: str, *, expected_sha: str, expected_head_sha: str, second_parent_sha: str = ""
) -> str:
    """Verify a checked-out commit and, for PR merges, its expected PR head parent."""
    if resolved_sha != expected_sha:
        raise SourceRevisionError(f"checked out {resolved_sha}, expected {expected_sha}")
    if expected_head_sha and second_parent_sha != expected_head_sha:
        raise SourceRevisionError(
            f"merge commit {resolved_sha} has second parent {second_parent_sha or 'none'}, "
            f"expected PR head {expected_head_sha}"
        )
    return resolved_sha


def verify_checked_out_source() -> None:
    """Validate the current checkout and expose its SHA to following workflow steps."""
    expected_sha = os.environ.get("SOURCE_COMMIT") or os.environ["GITHUB_SHA"]
    expected_head_sha = os.environ.get("EXPECTED_HEAD_SHA", "")
    try:
        resolved_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        second_parent_sha = ""
        if expected_head_sha:
            with suppress(subprocess.CalledProcessError):
                second_parent_sha = subprocess.check_output(["git", "rev-parse", "HEAD^2"], text=True).strip()
        verify_source_revision(
            resolved_sha,
            expected_sha=expected_sha,
            expected_head_sha=expected_head_sha,
            second_parent_sha=second_parent_sha,
        )
    except (OSError, subprocess.CalledProcessError, SourceRevisionError) as error:
        print(f"::error::{error}")
        raise SystemExit(1) from error

    _write_output(os.environ.get("GITHUB_OUTPUT", ""), "sha", resolved_sha)
    print(f"Resolved source revision: {resolved_sha}")


def resolve_pull_request_merge_commit() -> None:
    """Publish the merge commit to build as the ``sha`` output; no output means there is nothing to build."""
    number = int(os.environ["PR_NUMBER"])
    head_sha = os.environ["PR_HEAD_SHA"]
    api = GitHubApi(repository=os.environ["GITHUB_REPOSITORY"], token=os.environ["GITHUB_TOKEN"])
    try:
        outcome = resolve_merge_commit(api, number=number, head_sha=head_sha)
    except MergeCommitUnavailableError as error:
        print(f"::error::{error}")
        raise SystemExit(1) from error
    if isinstance(outcome, Resolved):
        _write_output(os.environ.get("GITHUB_OUTPUT", ""), "sha", outcome.merge_sha)
        print(f"PR #{number} head {head_sha} merges as {outcome.merge_sha}")
    else:
        print(f"::notice::{outcome.detail}")


def _write_output(path: str, key: str, value: str) -> None:
    if path:
        with Path(path).open("a", encoding="utf-8") as output:
            output.write(f"{key}={value}\n")


def main() -> None:
    match sys.argv[1:]:
        case ["resolve"]:
            resolve_pull_request_merge_commit()
        case ["verify-source"]:
            verify_checked_out_source()
        case args:
            raise ValueError(f"usage: pr_mergeability.py (resolve | verify-source), got {args=}")


if __name__ == "__main__":
    main()

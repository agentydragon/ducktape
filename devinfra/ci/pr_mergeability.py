"""Report whether GitHub can produce the current PR merge commit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

CHECK_NAME = "PR mergeability"
MAX_ATTEMPTS = 4


class GitHubApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SourceRevisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Assessment:
    head_sha: str
    conclusion: Literal["success", "failure"]
    title: str
    summary: str

    @property
    def mergeable(self) -> bool:
        return self.conclusion == "success"


class GitHubApi:
    def __init__(self, *, repository: str, token: str, api_url: str | None = None) -> None:
        self.repository = repository
        self.token = token
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_url}/repos/{self.repository}/{path.lstrip('/')}"
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "ducktape-pr-mergeability",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise GitHubApiError(f"GitHub API returned HTTP {error.code}: {detail}", status=error.code) from error
        except URLError as error:
            raise GitHubApiError(f"Could not reach the GitHub API: {error.reason}") from error

    def get_pull_request(self, number: int) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("GET", f"pulls/{number}"))

    def get_git_commit(self, sha: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("GET", f"git/commits/{sha}"))

    def get_git_ref(self, ref: str) -> dict[str, Any]:
        return cast(dict[str, Any], self.request("GET", f"git/ref/heads/{quote(ref, safe='/')}"))

    def list_open_pull_requests(self, base_branch: str) -> list[dict[str, Any]]:
        pull_requests: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urlencode({"state": "open", "base": base_branch, "per_page": 100, "page": page})
            page_items = cast(list[dict[str, Any]], self.request("GET", f"pulls?{query}"))
            pull_requests.extend(pull for pull in page_items if not pull.get("draft", False))
            if len(page_items) < 100:
                return pull_requests
            page += 1

    def get_check_runs(self, commit_sha: str) -> list[dict[str, Any]]:
        query = urlencode({"check_name": CHECK_NAME, "per_page": 100})
        response = cast(dict[str, Any], self.request("GET", f"commits/{commit_sha}/check-runs?{query}"))
        return cast(list[dict[str, Any]], response.get("check_runs", []))

    def create_check_run(self, payload: dict[str, Any]) -> None:
        self.request("POST", "check-runs", payload)

    def update_check_run(self, check_run_id: int, payload: dict[str, Any]) -> None:
        self.request("PATCH", f"check-runs/{check_run_id}", payload)


def verify_source_revision(
    resolved_sha: str, *, source_ref: str, expected_sha: str, expected_head_sha: str, second_parent_sha: str = ""
) -> str:
    """Verify a checked-out commit and, for PR merges, its expected PR head parent."""
    if not source_ref and resolved_sha != expected_sha:
        raise SourceRevisionError(f"checked out {resolved_sha}, expected {expected_sha}")
    if expected_head_sha and second_parent_sha != expected_head_sha:
        actual_head = second_parent_sha or "unknown"
        remediation = (
            "The live source ref no longer matches this workflow's event-time PR head; start a fresh PR workflow "
            "after GitHub synchronizes the PR."
            if source_ref
            else "Wait for GitHub to refresh the merge ref and start a new run; rerunning this run reuses the old source."
        )
        raise SourceRevisionError(
            f"GitHub's synthetic merge is stale for this PR head (merge parent {actual_head}; "
            f"expected PR head {expected_head_sha}). This is a PR source-sync failure, not a Bazel or Nix test failure. "
            f"{remediation}"
        )
    return resolved_sha


def verify_checked_out_source() -> None:
    """Validate the current checkout and expose its SHA to following workflow steps."""
    source_ref = os.environ.get("SOURCE_REF", "")
    expected_sha = os.environ.get("SOURCE_COMMIT") or os.environ.get("GITHUB_SHA", "")
    expected_head_sha = os.environ.get("EXPECTED_HEAD_SHA", "")
    try:
        resolved_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        second_parent_sha = ""
        if expected_head_sha:
            with suppress(subprocess.CalledProcessError):
                second_parent_sha = subprocess.check_output(["git", "rev-parse", "HEAD^2"], text=True).strip()
        verify_source_revision(
            resolved_sha,
            source_ref=source_ref,
            expected_sha=expected_sha,
            expected_head_sha=expected_head_sha,
            second_parent_sha=second_parent_sha,
        )
    except (OSError, subprocess.CalledProcessError, SourceRevisionError) as error:
        print(f"::error::{error}")
        raise SystemExit(1) from error

    _write_output(os.environ.get("GITHUB_OUTPUT", ""), "sha", resolved_sha)
    print(f"Resolved source revision: {resolved_sha}")


def inspect_pull_request(
    api: Any,
    *,
    number: int,
    fallback_head_sha: str = "",
    attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> Assessment:
    """Wait briefly for GitHub's mergeability calculation and verify both merge parents."""
    last_head_sha = fallback_head_sha
    last_problem = "GitHub has not finished calculating whether this pull request is mergeable."
    last_was_mergeable = False

    for attempt in range(attempts):
        try:
            pull = api.get_pull_request(number)
        except GitHubApiError as error:
            return Assessment(
                head_sha=last_head_sha,
                conclusion="failure",
                title="Could not verify PR mergeability",
                summary=(
                    f"GitHub could not provide current mergeability for PR #{number}. "
                    f"This is a source-status check failure, not a Bazel or Nix test result.\n\n{error}"
                ),
            )

        head_sha = pull.get("head", {}).get("sha", "")
        base_sha = pull.get("base", {}).get("sha", "")
        base_ref = pull.get("base", {}).get("ref", "devel")
        last_head_sha = head_sha or last_head_sha
        if not head_sha or not base_sha:
            last_problem = "GitHub's pull request response omitted the current head or base SHA."
            last_was_mergeable = False
        elif pull.get("mergeable") is False:
            mergeable_state = pull.get("mergeable_state", "unknown")
            if mergeable_state == "dirty":
                title = f"PR #{number} conflicts with {base_ref}"
                summary = (
                    f"GitHub reports that PR #{number} cannot be cleanly merged with `{base_ref}`.\n\n"
                    f"- PR head: `{head_sha}`\n"
                    f"- Current base: `{base_sha}`\n"
                    f"- GitHub merge state: `{mergeable_state}`\n\n"
                    "Resolve the conflict with the current base and push the updated PR branch. "
                    "Rerunning this CI run will reuse its recorded merge source and will not resolve the conflict. "
                    "No Bazel or Nix work was started by this mergeability check."
                )
            else:
                title = f"PR #{number} is not mergeable"
                summary = (
                    f"GitHub reports `mergeable=false` for PR #{number} against `{base_ref}` "
                    f"(merge state `{mergeable_state}`).\n\n"
                    f"- PR head: `{head_sha}`\n"
                    f"- Current base: `{base_sha}`\n\n"
                    "This is a PR source-state failure, not a Bazel or Nix test result. "
                    "Inspect the PR merge state before rerunning CI."
                )
            return Assessment(head_sha, "failure", title, summary)
        elif pull.get("mergeable") is True:
            last_was_mergeable = True
            merge_sha = pull.get("merge_commit_sha")
            if not merge_sha:
                last_problem = "GitHub reports the PR as mergeable but has not supplied its synthetic merge commit SHA."
            else:
                try:
                    merge_commit = api.get_git_commit(merge_sha)
                except GitHubApiError as error:
                    last_problem = f"GitHub's synthetic merge commit `{merge_sha}` is not available: {error}"
                else:
                    parents = [parent.get("sha", "") for parent in merge_commit.get("parents", [])]
                    try:
                        live_base_sha = api.get_git_ref(base_ref).get("object", {}).get("sha", "")
                    except GitHubApiError as error:
                        last_problem = f"GitHub's current `{base_ref}` ref is not available: {error}"
                        live_base_sha = ""
                    if live_base_sha and parents == [live_base_sha, head_sha]:
                        return Assessment(
                            head_sha,
                            "success",
                            f"PR #{number} is mergeable with {base_ref}",
                            (
                                f"GitHub's current synthetic merge commit is available and has the expected parents.\n\n"
                                f"- PR head: `{head_sha}`\n"
                                f"- Current base: `{live_base_sha}`\n"
                                f"- PR base metadata: `{base_sha}`\n"
                                f"- Synthetic merge: `{merge_sha}`\n"
                                f"- Merge parents: `{parents[0]}` + `{parents[1]}`"
                            ),
                        )
                    if live_base_sha:
                        last_problem = (
                            f"GitHub reports the PR as mergeable, but synthetic merge `{merge_sha}` has parents "
                            f"`{', '.join(parents) or 'none'}`; expected live `{base_ref}` ref `{live_base_sha}` "
                            f"and PR head `{head_sha}` (PR base metadata says `{base_sha}`)."
                        )
        else:
            last_problem = "GitHub has not finished calculating whether this pull request is mergeable."
            last_was_mergeable = False

        if attempt + 1 < attempts:
            sleep(min(2**attempt, 4))

    if last_was_mergeable:
        title = "GitHub synthetic merge is stale or unavailable"
        summary = (
            f"GitHub reports PR #{number} as mergeable, but its current synthetic merge could not be verified "
            f"after {attempts} checks.\n\n{last_problem}\n\n"
            "This is a GitHub merge-source consistency failure, not a Bazel or Nix test result. "
            "Wait for GitHub to refresh the merge ref, then start a new run; rerunning an older run reuses its source SHA."
        )
    else:
        title = "GitHub mergeability is still unknown"
        summary = (
            f"GitHub did not finish calculating mergeability for PR #{number} after {attempts} checks.\n\n"
            f"{last_problem}\n\n"
            "This is a GitHub source-status delay, not a Bazel or Nix test result. "
            "Retry the mergeability check after GitHub finishes calculating the merge."
        )
    return Assessment(last_head_sha, "failure", title, summary)


def publish_check(api: Any, *, number: int, result: Assessment, details_url: str) -> None:
    if not result.head_sha:
        raise ValueError(f"cannot publish PR #{number} mergeability without a head SHA")
    external_id = f"pr-mergeability:{number}"
    payload: dict[str, Any] = {
        "name": CHECK_NAME,
        "status": "completed",
        "conclusion": result.conclusion,
        "output": {"title": result.title[:255], "summary": result.summary[:60000]},
        "details_url": details_url,
        "external_id": external_id,
    }
    existing = [run for run in api.get_check_runs(result.head_sha) if run.get("external_id") == external_id]
    if existing:
        latest = max(existing, key=lambda run: run.get("id", 0))
        api.update_check_run(latest["id"], payload)
    else:
        api.create_check_run({"head_sha": result.head_sha, **payload})


def _write_output(path: str, key: str, value: str) -> None:
    if path:
        with Path(path).open("a", encoding="utf-8") as output:
            output.write(f"{key}={value}\n")


def _write_summary(path: str, number: int, result: Assessment, url: str) -> None:
    if path:
        with Path(path).open("a", encoding="utf-8") as summary:
            summary.write(f"## {result.title}\n\nPR: [#{number}]({url})\n\n{result.summary}\n\n")


def main() -> None:
    if sys.argv[1:] == ["verify-source"]:
        verify_checked_out_source()
        return
    if sys.argv[1:]:
        raise ValueError(f"unsupported command: {' '.join(sys.argv[1:])}")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repository:
        raise ValueError("GITHUB_TOKEN and GITHUB_REPOSITORY are required")

    api = GitHubApi(repository=repository, token=token)
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    pull_number = os.environ.get("PR_NUMBER", "")
    base_branch = os.environ.get("BASE_BRANCH", "devel")
    if event == "pull_request_target" and pull_number:
        pull_requests: list[dict[str, Any]] = [
            {"number": int(pull_number), "head": {"sha": os.environ.get("PR_HEAD_SHA", "")}}
        ]
    elif event in {"push", "schedule", "workflow_dispatch"}:
        pull_requests = api.list_open_pull_requests(base_branch)
    else:
        raise ValueError(f"unsupported event/PR number combination: {event!r}, {pull_number!r}")

    if not pull_requests:
        print(f"No open, non-draft PRs target {base_branch}.")
        _write_output(os.environ.get("GITHUB_OUTPUT", ""), "mergeable", "true")
        return

    run_url = (
        f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/actions/runs/"
        f"{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    for pull in pull_requests:
        number = int(pull["number"])
        pull_url: str = pull.get("html_url") or (
            f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/pull/{number}"
        )
        result = inspect_pull_request(api, number=number, fallback_head_sha=pull.get("head", {}).get("sha", ""))
        publish_check(api, number=number, result=result, details_url=run_url)
        _write_summary(os.environ.get("GITHUB_STEP_SUMMARY", ""), number, result, pull_url)
        print(f"PR #{number}: {result.title} ({result.conclusion})")
        if event == "pull_request_target":
            _write_output(os.environ.get("GITHUB_OUTPUT", ""), "mergeable", str(result.mergeable).lower())


if __name__ == "__main__":
    main()

"""Apply visual-review comment updates to a pull request's singleton bot comment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from github import Auth, Github

from devinfra.pr_visuals.github_comment import COMMENT_MARKER


@dataclass(frozen=True)
class CommentUpdate:
    body: str
    mode: Literal["replace", "refresh_stale"]


def _is_current_success_comment(*, body: str, repository: str, commit_sha: str) -> bool:
    commit_url = f"https://github.com/{repository}/commit/{commit_sha}"
    html_heading = f'<h2>Visual review for <a href="{commit_url}"><code>{commit_sha[:8]}</code></a></h2>'
    markdown_heading = f"## Visual review for [`{commit_sha[:8]}`]({commit_url})"
    return (html_heading in body and ">Open visual review</a>" in body) or (
        markdown_heading in body and "[Open visual review](" in body
    )


def _find_pull_request_comment(issue: Any) -> Any | None:
    return next(
        (
            comment
            for comment in issue.get_comments()
            if comment.user is not None and comment.user.type == "Bot" and COMMENT_MARKER in (comment.body or "")
        ),
        None,
    )


def publish_pull_request_comment(
    *, repository: str, pull_request: int, commit_sha: str, update: CommentUpdate, token: str
) -> None:
    """Replace the singleton, or refresh it only if it is stale.

    A successful no-artifact rerun has no manifest to republish. Preserve an
    existing review for this SHA; with no singleton, leave the PR comment-free.
    """
    with Github(auth=Auth.Token(token)) as github:
        issue = github.get_repo(repository).get_issue(pull_request)
        existing = _find_pull_request_comment(issue)
        if update.mode == "refresh_stale":
            if existing is not None and not _is_current_success_comment(
                body=existing.body or "", repository=repository, commit_sha=commit_sha
            ):
                existing.edit(update.body)
        elif existing is None:
            issue.create_comment(update.body)
        else:
            existing.edit(update.body)

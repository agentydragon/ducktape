"""Apply visual-review comment updates to a pull request's singleton bot comment."""

from __future__ import annotations

from dataclasses import dataclass

from github import Github
from github.Issue import Issue
from github.IssueComment import IssueComment

from devinfra.pr_visuals.github_comment import COMMENT_MARKER, COMMENT_METADATA_PREFIX, CommentState


@dataclass(frozen=True)
class CommentUpdate:
    body: str
    state: CommentState

    def render(self) -> str:
        return f"{self.state.metadata()}\n{self.body}"


def _find_pull_request_comment(issue: Issue) -> IssueComment | None:
    return next(
        (
            comment
            for comment in issue.get_comments()
            if comment.user is not None
            and comment.user.type == "Bot"
            and (COMMENT_MARKER in (comment.body or "") or COMMENT_METADATA_PREFIX in (comment.body or ""))
        ),
        None,
    )


def publish_pull_request_comment(*, github: Github, repository: str, pull_request: int, update: CommentUpdate) -> None:
    """Publish the current result, avoiding a new comment for a successful empty run."""
    issue = github.get_repo(repository).get_issue(pull_request)
    existing = _find_pull_request_comment(issue)
    if existing is None and update.state.kind == "no_visual" and update.state.ci_conclusion == "success":
        return
    if existing is None:
        issue.create_comment(update.render())
    else:
        existing.edit(update.render())

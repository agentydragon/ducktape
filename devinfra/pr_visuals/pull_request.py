"""Find the open or merged pull request a CI run was built for.

A `workflow_run` event's `pull_requests` is empty for a fork's run, and for any run it is resolved when
the event is delivered, not for the run's commit; so the PR is found from the run's head ref and commit.
"""

from __future__ import annotations

from dataclasses import dataclass

from github import Github
from more_itertools import only


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    base_sha: str


def find_reviewable_pull_request(*, github: Github, repository: str, head: str, head_sha: str) -> PullRequestRef | None:
    """Find an open or merged PR whose final head is the run's exact commit.

    Closed-unmerged PRs and superseded commits cannot receive reviews. A merged
    PR can: publication may finish after merge. Ambiguous matches still raise.
    """
    pulls = github.get_repo(repository).get_pulls(state="all", head=head)
    pull = only([pull for pull in pulls if pull.head.sha == head_sha and (pull.state == "open" or pull.merged)])
    return None if pull is None else PullRequestRef(number=pull.number, base_sha=pull.base.sha)

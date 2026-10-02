"""Find the open pull request a CI run was built for.

A `workflow_run` event's `pull_requests` is empty for a fork's run, and for any run it is resolved when
the event is delivered, not for the run's commit; so the PR is found from the run's head ref and commit.
"""

from __future__ import annotations

from dataclasses import dataclass

from github import Auth, Github
from more_itertools import only


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    base_sha: str


def find_open_pull_request(*, repository: str, head: str, head_sha: str, token: str) -> PullRequestRef | None:
    """The open PR from ``head`` (``owner:branch``) whose head commit is ``head_sha``, if there is one.

    None means the run is for a commit that is no longer the head of an open PR: superseded by a newer push,
    or its PR closed. Two such PRs raise: attaching a review to the wrong one is worse than publishing none.
    """
    with Github(auth=Auth.Token(token)) as github:
        pulls = github.get_repo(repository).get_pulls(state="open", head=head)
        pull = only([pull for pull in pulls if pull.head.sha == head_sha])
        return None if pull is None else PullRequestRef(number=pull.number, base_sha=pull.base.sha)

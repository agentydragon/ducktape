"""GitHub interface for managing pull requests and remote operations.

This module uses PyGithub by default and optionally reads tokens from env or gh CLI.
No subprocess JSON parsing is used for API calls.
"""

import logging
import os
import subprocess

from github import Github
from github.Repository import Repository

from wt.shared.env import is_test_mode
from wt.shared.error_handling import GitHubUnavailableError, handle_github_errors
from wt.shared.github_models import GitHubPRResponse, HasBasicPR, PRState, PullRequestList

logger = logging.getLogger(__name__)


def get_github_token(token_arg: str | None = None, *, timeout_secs: float = 10.0) -> str | None:
    """Obtain a GitHub token from explicit arg, env, or gh CLI.

    Separated for easy mocking in tests: patch wt.server.github_client.get_github_token.
    Skips gh in WT_TEST_MODE to avoid network/process flakiness under test.
    """
    if token_arg:
        return token_arg
    if env_tok := os.environ.get("GITHUB_TOKEN"):
        return env_tok
    if is_test_mode():
        return None
    try:
        cp = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True, timeout=timeout_secs)
        tok = (cp.stdout or "").strip()
        return tok or None
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    except (OSError, PermissionError) as e:
        # Unexpected system errors should be visible to operator
        raise RuntimeError("Failed to execute GitHub CLI") from e


class GitHubInterface:
    def __init__(self, github_repo: str, token: str | None = None):
        token = get_github_token(token)
        self.github_repo = github_repo
        self._gh = Github(token) if token else Github()
        self._repo: Repository | None = None  # Lazy initialization

    @property
    def repo(self) -> Repository:
        """Lazy initialization of the GitHub repository object."""
        if self._repo is None:
            try:
                self._repo = self._gh.get_repo(self.github_repo)
            except Exception as e:
                # Boundary: wrap provider/library errors; log for diagnostics.
                logger.exception("Failed to access GitHub repo %s", self.github_repo)
                raise GitHubUnavailableError(f"Cannot access GitHub repo {self.github_repo}") from e
        return self._repo

    @handle_github_errors
    def pr_list(self) -> list[PullRequestList]:
        pulls = self.repo.get_pulls(state="all", sort="created", direction="desc")
        return [
            PullRequestList(
                number=pr.number,
                headRefName=pr.head.ref,
                state=PRState(pr.state),
                title=pr.title,
                mergedAt=(pr.merged_at.isoformat() if pr.merged_at else None),
            )
            for pr in pulls
        ]

    @handle_github_errors
    def pr_search(self, branch_name: str) -> list[HasBasicPR]:
        """Search for PRs by branch name using GitHub search API instead of paginating all PRs."""
        # Use GitHub search API to find PRs by head branch - much more efficient
        search_query = f"repo:{self.github_repo} type:pr head:{branch_name}"

        # Search for issues/PRs matching the branch
        issues = self._gh.search_issues(search_query)
        return [self.repo.get_pull(issue.number) for issue in issues]

    @handle_github_errors
    def pr_view(self, pr_number: int) -> GitHubPRResponse:
        pr = self.repo.get_pull(pr_number)
        return GitHubPRResponse.from_github_pr(pr)

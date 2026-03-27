"""Set up 'fork' git remote for GitHub token-based push access.

When a GITHUB_TOKEN is available, introspects the authenticated GitHub user,
checks whether their fork of the current repo exists, and if so ensures the
'fork' remote points at it with token-based HTTPS authentication embedded in
the URL.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import pygit2

logger = logging.getLogger(__name__)


class ForkRemoteAction(StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    ALREADY_CORRECT = "already_correct"
    FORK_NOT_FOUND = "fork_not_found"


@dataclass
class ForkRemoteSetup:
    """Result of fork remote setup."""

    username: str
    repo_name: str
    fork_exists: bool
    action: ForkRemoteAction


def _github_api_get(path: str, github_token: str) -> dict[str, Any]:
    """Make an authenticated GET request to the GitHub API."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "ducktape-session-hook/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return cast(dict[str, Any], json.loads(response.read()))


def _introspect_github_user(github_token: str) -> str:
    """Introspect GitHub username from token via the API."""
    return cast(str, _github_api_get("/user", github_token)["login"])


def _check_fork_exists(username: str, repo_name: str, github_token: str) -> bool:
    """Return True if the repo exists under the given username."""
    try:
        _github_api_get(f"/repos/{username}/{repo_name}", github_token)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def _get_repo_name_from_origin(project_dir: Path) -> str | None:
    """Extract repo name from the git origin remote URL."""
    try:
        repo = pygit2.Repository(str(project_dir))
    except pygit2.GitError:
        return None
    origin = next((r for r in repo.remotes if r.name == "origin"), None)
    if origin is None or origin.url is None:
        return None
    url = origin.url.rstrip("/")
    url = url.removesuffix(".git")
    return url.split("/")[-1]


def ensure_fork_remote(github_token: str, project_dir: Path) -> ForkRemoteSetup:
    """Ensure the 'fork' remote points at the authenticated user's fork with token auth.

    If the fork does not yet exist on GitHub, returns a result with
    fork_exists=False and does not modify git config.

    The embedded token allows password-less git push without a credential helper.
    """
    username = _introspect_github_user(github_token)
    repo_name = _get_repo_name_from_origin(project_dir)
    if repo_name is None:
        raise RuntimeError("Could not determine repo name from git origin remote")

    if not _check_fork_exists(username, repo_name, github_token):
        logger.warning("Fork not found at https://github.com/%s/%s — create it there first", username, repo_name)
        return ForkRemoteSetup(
            username=username, repo_name=repo_name, fork_exists=False, action=ForkRemoteAction.FORK_NOT_FOUND
        )

    fork_url = f"https://{username}:{github_token}@github.com/{username}/{repo_name}.git"

    repo = pygit2.Repository(str(project_dir))
    fork_remote = next((r for r in repo.remotes if r.name == "fork"), None)

    if fork_remote is None:
        repo.remotes.create("fork", fork_url)
        action = ForkRemoteAction.ADDED
    elif fork_remote.url == fork_url:
        action = ForkRemoteAction.ALREADY_CORRECT
    else:
        repo.remotes.set_url("fork", fork_url)
        action = ForkRemoteAction.UPDATED

    logger.info("Fork remote %s: https://github.com/%s/%s.git", action, username, repo_name)
    return ForkRemoteSetup(username=username, repo_name=repo_name, fork_exists=True, action=action)

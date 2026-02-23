"""Set up 'fork' git remote for GitHub token-based push access.

When a GITHUB_TOKEN is available, introspects the authenticated GitHub user,
checks whether their fork of the current repo exists, and if so ensures the
'fork' remote points at it with token-based HTTPS authentication embedded in
the URL.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)


@dataclass
class ForkRemoteSetup:
    """Result of fork remote setup."""

    username: str
    repo_name: str
    fork_exists: bool
    # action is set only when fork_exists is True
    action: str  # "added", "updated", "already_correct", "fork_not_found"


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
    """Extract repo name from git origin remote URL."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"], check=False, capture_output=True, text=True, cwd=project_dir
    )
    if result.returncode != 0:
        return None
    origin_url = result.stdout.strip()
    # Matches both HTTPS (owner/repo.git) and SSH (owner:repo.git) URL forms.
    match = re.search(r"[/:]([^/:]+?)(?:\.git)?$", origin_url)
    return match.group(1) if match else None


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
        return ForkRemoteSetup(username=username, repo_name=repo_name, fork_exists=False, action="fork_not_found")

    fork_url = f"https://{username}:{github_token}@github.com/{username}/{repo_name}.git"

    current = subprocess.run(
        ["git", "remote", "get-url", "fork"], check=False, capture_output=True, text=True, cwd=project_dir
    )
    current_url = current.stdout.strip() if current.returncode == 0 else None

    if current_url is None:
        subprocess.run(["git", "remote", "add", "fork", fork_url], check=True, cwd=project_dir)
        action = "added"
    elif current_url == fork_url:
        action = "already_correct"
    else:
        subprocess.run(["git", "remote", "set-url", "fork", fork_url], check=True, cwd=project_dir)
        action = "updated"

    logger.info("Fork remote %s: https://github.com/%s/%s.git", action, username, repo_name)
    return ForkRemoteSetup(username=username, repo_name=repo_name, fork_exists=True, action=action)

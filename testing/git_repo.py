"""Build disposable Git repositories for tests that exercise the Git CLI."""

import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    """Run Git in a directory and return stdout with its original whitespace."""
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def configure_git_author(repo: Path) -> None:
    """Set a local test identity and disable signing in a disposable checkout."""
    git(repo, "config", "--local", "user.name", "Test Author")
    git(repo, "config", "--local", "user.email", "test@example.invalid")
    git(repo, "config", "--local", "commit.gpgSign", "false")


def init_git_repo(repo: Path, *, branch: str = "main") -> None:
    """Initialize an empty working repository in an existing directory."""
    git(repo, "init", "--quiet", "--initial-branch", branch)
    configure_git_author(repo)

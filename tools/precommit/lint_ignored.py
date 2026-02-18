"""Shared utility for checking whether files are lint-ignored via gitattributes.

Files are excluded from linting when any of LINT_IGNORED_ATTRS is set to true
in .gitattributes. This is the single source of truth used by both the unified
precommit runner and standalone lint tools (e.g. check_pytest_main).
"""

from pathlib import Path

import pygit2

LINT_IGNORED_ATTRS = ("linguist-generated", "gitlab-generated", "rules-lint-ignored")


def is_lint_ignored(repo: pygit2.Repository, path: Path) -> bool:
    """Return True if path is marked as lint-ignored in .gitattributes."""
    return any(repo.get_attr(str(path), attr) in (True, "true") for attr in LINT_IGNORED_ATTRS)


def try_open_repo(path: Path) -> pygit2.Repository | None:
    """Try to open the git repository at or above path.

    Returns None if path is not inside a git repository (e.g. inside a Bazel
    sandbox test action where no .git directory is present).
    """
    try:
        return pygit2.Repository(str(path))
    except pygit2.GitError:
        return None

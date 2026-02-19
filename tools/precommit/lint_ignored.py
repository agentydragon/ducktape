"""Shared utility for checking whether files are lint-ignored via gitattributes.

Files are excluded from linting when any of LINT_IGNORED_ATTRS is set to true
in .gitattributes.
"""

from pathlib import Path

import pygit2

LINT_IGNORED_ATTRS = ("linguist-generated", "gitlab-generated", "rules-lint-ignored")


def is_lint_ignored(repo: pygit2.Repository, path: Path) -> bool:
    """Return True if path is marked as lint-ignored in .gitattributes."""
    return any(repo.get_attr(str(path), attr) in (True, "true") for attr in LINT_IGNORED_ATTRS)

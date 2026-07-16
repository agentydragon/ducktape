"""Standardized error handling for wt (lean, used-only surface)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from wt.shared.constants import RESERVED_NAMES
from wt.shared.github_models import GitHubError

logger = logging.getLogger(__name__)


class WorktreeManagerError(Exception):
    pass


class GitHubUnavailableError(WorktreeManagerError):
    pass


def handle_github_errors[T](func: Callable[..., T]) -> Callable[..., T]:
    @wraps(func)
    def wrapper(*args, **kwargs) -> T:
        try:
            return func(*args, **kwargs)
        except GitHubError as e:
            logger.warning("GitHub API error in %s: %s", func.__name__, e)
            raise GitHubUnavailableError(f"GitHub API failed: {e}") from e

    return wrapper


def validate_worktree_name(name: str) -> None:
    if not name:
        raise WorktreeManagerError("Worktree name cannot be empty")
    if name in RESERVED_NAMES:
        raise WorktreeManagerError(f"Cannot use reserved name: {name}")
    if "/" in name or "\\" in name:
        raise WorktreeManagerError(f"Worktree name cannot contain path separators: {name}")


def log_operation_error(operation: str, worktree_name: str, error: Exception, **context: Any) -> None:
    logger.error(
        "Operation %s failed for worktree %s: %s",
        operation,
        worktree_name,
        error,
        extra={
            "operation": operation,
            "worktree": worktree_name,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": context,
        },
    )


class ErrorContext:
    def __init__(self, operation: str, worktree_name: str = ""):
        self.operation = operation
        self.worktree_name = worktree_name

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            log_operation_error(self.operation, self.worktree_name, exc_val)
        return False  # Do not suppress exceptions

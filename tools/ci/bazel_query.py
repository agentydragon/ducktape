"""Shared Bazel query utilities for CI scripts."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from bazel_util.query import run_query as _run_query

logger = logging.getLogger(__name__)

_BAZEL_QUERY_LOG_DIR_ENV = "BAZEL_QUERY_LOG_DIR"
_DEFAULT_QUERY_LOG_DIR = "/tmp/bazel-query-logs"


def _make_persist_dir() -> Path:
    """Create and return a timestamped per-query directory under BAZEL_QUERY_LOG_DIR."""
    query_log_dir = Path(os.environ.get(_BAZEL_QUERY_LOG_DIR_ENV, _DEFAULT_QUERY_LOG_DIR))
    logger.info(
        "Saving query to: %s (env %s=%s)",
        query_log_dir,
        _BAZEL_QUERY_LOG_DIR_ENV,
        os.environ.get(_BAZEL_QUERY_LOG_DIR_ENV),
    )
    query_log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H%M%S")
    query_dir = query_log_dir / f"{timestamp}_{uuid.uuid4().hex[:8]}"
    query_dir.mkdir()
    return query_dir


def run_query(query: str) -> list[str]:
    """Run a bazel query and return matching targets as label strings.

    Raises CalledProcessError on failure.
    """
    labels = _run_query(query, persist_dir=_make_persist_dir())
    return [str(label) for label in labels]


def query_with_targets(query_template: str, targets: list[str]) -> list[str]:
    """Run a Bazel query template with ``$targets`` replaced by the target set.

    Returns matching targets, or an empty list if targets is empty.
    """
    if not targets:
        return []

    target_set = f"set({' '.join(targets)})"
    query = query_template.replace("$targets", target_set)
    return run_query(query)


def filter_for_ci(targets: list[str]) -> list[str]:
    """Filter targets for CI: keep only buildable, compatible, non-manual targets.

    Combines three filters into a single bazel query invocation:
    - kind('rule', ...) — exclude source file labels (not buildable)
    - except attr(target_compatible_with, macos) — exclude platform-incompatible
    - except attr(tags, 'manual') — exclude targets needing special setup
      (e.g. system libraries). Release workflows build these explicitly.
    """
    if not targets:
        return targets

    target_set = f"set({' '.join(targets)})"
    query = (
        f"let targets = {target_set} in "
        f"kind('rule', $targets) "
        f"except attr(target_compatible_with, '@platforms//os:macos', $targets) "
        f"except attr(tags, 'manual', $targets)"
    )
    return run_query(query)

"""Compute release decisions using bazel-diff.

Compares against last release tag for a specific package to determine
if a new release is needed.

This module provides the implementation logic. See check_release.py for the CLI entry point.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pygit2
from pydantic import BaseModel

from tools.ci.diff_utils import download_bazel_diff, get_changed_files, has_infra_changes, run_bazel_diff
from tools.ci.github_actions import CIEnvironment
from tools.env_utils import get_required_env

logger = logging.getLogger(__name__)


class ReleaseEnvironment(BaseModel):
    """Environment for release checks."""

    ci: CIEnvironment
    package_prefix: str
    wheel_target: str
    latest_release_tag: str

    @classmethod
    def from_env(cls) -> ReleaseEnvironment:
        """Load release environment from os.environ."""
        return cls(
            ci=CIEnvironment.from_env(),
            package_prefix=get_required_env("PACKAGE_PREFIX"),
            wheel_target=get_required_env("BAZEL_WHEEL_TARGET"),
            latest_release_tag=get_required_env("LATEST_RELEASE_TAG"),
        )


def get_last_release_commit(repo: pygit2.Repository, latest_release_tag: str) -> pygit2.Commit | None:
    """Find the commit of the last release by looking up the floating latest tag."""
    ref = repo.references.get(f"refs/tags/{latest_release_tag}")
    if ref is None:
        logger.info("No existing release tag '%s' found", latest_release_tag)
        return None
    commit = ref.peel(pygit2.Commit)
    logger.info("Found release tag '%s' at %s", latest_release_tag, str(commit.id)[:8])
    return commit


def compute_release_decision(env: ReleaseEnvironment, repo: pygit2.Repository) -> bool:
    """Compute whether a release is needed for a package.

    Checks if the specific wheel target is in the affected targets list.
    """
    base_commit = get_last_release_commit(repo, env.latest_release_tag)

    if not base_commit:
        logger.info("First release (no previous release found)")
        return True

    logger.info("Last release commit: %s", str(base_commit.id)[:8])

    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files since last release: %d", len(changed_files))

    if has_infra_changes(changed_files):
        logger.info("Infrastructure files changed, assuming release needed")
        return True

    jar_path = Path(os.environ.get("BAZEL_DIFF_JAR", "/tmp/bazel-diff.jar"))
    download_bazel_diff(jar_path)

    cache_dir = env.ci.workspace / ".bazel-diff-cache"
    targets = run_bazel_diff(repo, jar_path, env.ci.workspace, base_commit, cache_dir)
    logger.info("Found %d affected targets total", len(targets))

    needed = env.wheel_target in targets
    logger.info("Target %s %s", env.wheel_target, "changed" if needed else "not in affected targets")
    return needed


def main() -> None:
    """Main entry point - check if release is needed for a specific package."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])

    env = ReleaseEnvironment.from_env()

    logger.info("Checking if release needed for %s", env.package_prefix)
    logger.info("Wheel target: %s", env.wheel_target)
    logger.info("Latest release tag: %s", env.latest_release_tag)

    repo = pygit2.Repository(env.ci.workspace)
    release_needed = compute_release_decision(env, repo)
    env.ci.write_outputs({"release_needed": release_needed})

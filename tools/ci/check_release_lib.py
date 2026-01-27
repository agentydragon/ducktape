"""Compute release decisions using bazel-diff.

Compares against last release tag for a specific package to determine
if a new release is needed.

This module provides the implementation logic. See check_release.py for the CLI entry point.
"""

from __future__ import annotations

import logging
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

    @classmethod
    def from_env(cls) -> ReleaseEnvironment:
        """Load release environment from os.environ."""
        return cls(
            ci=CIEnvironment.from_env(),
            package_prefix=get_required_env("PACKAGE_PREFIX"),
            wheel_target=get_required_env("BAZEL_TARGET_PATTERN"),
        )


def get_last_release_commit(repo: pygit2.Repository, package_prefix: str) -> pygit2.Commit | None:
    """Find the commit of the last release for a package using pygit2."""
    # Get all tags matching the pattern
    matching_tags = []
    for ref_name in repo.references:
        if ref_name.startswith("refs/tags/") and package_prefix in ref_name:
            tag_name = ref_name.replace("refs/tags/", "")
            if tag_name.startswith(f"{package_prefix}-") and "latest" not in tag_name:
                matching_tags.append((tag_name, ref_name))

    if not matching_tags:
        return None

    # Sort by creatordate (most recent first) - get commit time
    def get_commit_time(tag_info: tuple[str, str]) -> int:
        ref = repo.references.get(tag_info[1])
        if ref is None:
            return 0
        target = ref.peel(pygit2.Commit)
        return target.commit_time

    matching_tags.sort(key=get_commit_time, reverse=True)

    latest_tag = matching_tags[0][0]
    logger.info("Found last release tag: %s", latest_tag)

    ref = repo.references.get(f"refs/tags/{latest_tag}")
    if ref is None:
        return None
    return ref.peel(pygit2.Commit)


def compute_release_decision(env: ReleaseEnvironment, repo: pygit2.Repository) -> bool:
    """Compute whether a release is needed for a package.

    Checks if the specific wheel target is in the affected targets list.
    """
    base_commit = get_last_release_commit(repo, env.package_prefix)

    if not base_commit:
        logger.info("First release (no previous release found)")
        return True

    logger.info("Last release commit: %s", str(base_commit.id)[:8])

    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files since last release: %d", len(changed_files))

    if has_infra_changes(changed_files):
        logger.info("Infrastructure files changed, assuming release needed")
        return True

    jar_path = Path("/tmp/bazel-diff.jar")
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

    repo = pygit2.Repository(env.ci.workspace)
    release_needed = compute_release_decision(env, repo)
    env.ci.write_outputs({"release_needed": release_needed})

"""Compute release decisions using bazel-diff.

Compares against last release tag for a specific package to determine
if a new release is needed.

This module provides the implementation logic. See check_release.py for the CLI entry point.
"""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

import pygit2
from pydantic import BaseModel

from tools.ci.diff_utils import get_changed_files, has_infra_changes, run_bazel_diff
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


BAZEL_DIFF_VERSION = "12.1.1"
BAZEL_DIFF_URL = f"https://github.com/Tinder/bazel-diff/releases/download/{BAZEL_DIFF_VERSION}/bazel-diff_deploy.jar"


def download_bazel_diff(dest: Path) -> None:
    """Download bazel-diff JAR if not already present.

    Raises on download failure.
    """
    if dest.exists():
        logger.info("bazel-diff already downloaded at %s", dest)
        return

    logger.info("Downloading bazel-diff v%s...", BAZEL_DIFF_VERSION)
    urllib.request.urlretrieve(BAZEL_DIFF_URL, dest)
    logger.info("Downloaded to %s", dest)


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


class ReleaseDecision(BaseModel):
    """Result of release decision computation."""

    release_needed: bool
    base_sha: str
    reason: str

    def to_outputs(self) -> dict[str, str | bool]:
        return {"release_needed": self.release_needed, "base_sha": self.base_sha, "reason": self.reason}


def compute_release_decision(env: ReleaseEnvironment, repo: pygit2.Repository) -> ReleaseDecision:
    """Compute whether a release is needed for a package.

    Checks if the specific wheel target is in the affected targets list.
    """
    base_commit = get_last_release_commit(repo, env.package_prefix)

    if not base_commit:
        return ReleaseDecision(release_needed=True, base_sha="", reason="first release (no previous release found)")

    base_sha = str(base_commit.id)
    logger.info("Last release commit: %s", base_sha)

    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files since last release: %d", len(changed_files))

    if has_infra_changes(changed_files):
        return ReleaseDecision(
            release_needed=True, base_sha=base_sha, reason="infrastructure files changed, assuming release needed"
        )

    jar_path = Path("/tmp/bazel-diff.jar")
    download_bazel_diff(jar_path)

    cache_dir = env.ci.workspace / ".bazel-diff-cache"
    targets = run_bazel_diff(repo, jar_path, env.ci.workspace, base_commit, cache_dir)

    if targets is None:
        return ReleaseDecision(
            release_needed=True, base_sha=base_sha, reason="bazel-diff failed, assuming release needed"
        )

    if not targets:
        return ReleaseDecision(
            release_needed=False, base_sha=base_sha, reason="no Bazel targets affected since last release"
        )

    logger.info("Found %d affected targets total", len(targets))

    if env.wheel_target not in targets:
        return ReleaseDecision(
            release_needed=False, base_sha=base_sha, reason=f"target {env.wheel_target} not in affected targets"
        )

    logger.info("Target %s is affected", env.wheel_target)
    return ReleaseDecision(release_needed=True, base_sha=base_sha, reason=f"target {env.wheel_target} changed")


def main() -> None:
    """Main entry point - check if release is needed for a specific package."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])

    env = ReleaseEnvironment.from_env()

    logger.info("Checking if release needed for %s", env.package_prefix)
    logger.info("Wheel target: %s", env.wheel_target)

    repo = pygit2.Repository(env.ci.workspace)
    decision = compute_release_decision(env, repo)
    env.ci.write_outputs(decision.to_outputs())

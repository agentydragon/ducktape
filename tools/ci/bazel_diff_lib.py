"""Compute release decisions using bazel-diff.

Compares against last release tag for a specific package to determine
if a new release is needed.

This module provides the implementation logic. See bazel_diff.py for the CLI entry point.
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.request
from pathlib import Path

import pygit2

from tools.ci.bazel_query import query_intersection
from tools.ci.diff_utils import get_changed_files, has_infra_changes, run_bazel_diff
from tools.ci.github_actions import bool_output, get_workspace, write_outputs

logger = logging.getLogger(__name__)

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


def _release_output(needed: bool, base_sha: str, reason: str) -> dict[str, str]:
    """Build release mode output dict."""
    return {"release_needed": bool_output(needed), "base_sha": base_sha, "reason": reason}


def compute_release_decision(
    repo: pygit2.Repository, workspace: Path, package_prefix: str, target_pattern: str
) -> dict[str, str]:
    """Compute whether a release is needed for a package.

    Returns outputs dict with release_needed, base_sha, and reason.
    """
    base_commit = get_last_release_commit(repo, package_prefix)

    if not base_commit:
        return _release_output(True, "", "first release (no previous release found)")

    base_sha = str(base_commit.id)
    logger.info("Last release commit: %s", base_sha)

    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files since last release: %d", len(changed_files))

    if has_infra_changes(changed_files):
        return _release_output(True, base_sha, "infrastructure files changed, assuming release needed")

    jar_path = Path("/tmp/bazel-diff.jar")
    download_bazel_diff(jar_path)

    targets = run_bazel_diff(repo, jar_path, workspace, base_commit)

    if targets is None:
        return _release_output(True, base_sha, "bazel-diff failed, assuming release needed")

    if not targets:
        return _release_output(False, base_sha, "no Bazel targets affected since last release")

    logger.info("Found %d affected targets total", len(targets))

    matching = query_intersection(targets, target_pattern)
    if not matching:
        return _release_output(False, base_sha, f"no targets matching {target_pattern} changed since last release")

    logger.info("Found %d matching targets:", len(matching))
    for t in matching[:10]:
        logger.info("  %s", t)
    if len(matching) > 10:
        logger.info("  ... and %d more", len(matching) - 10)

    return _release_output(True, base_sha, f"{len(matching)} targets matching {target_pattern} changed")


def main() -> None:
    """Main entry point - check if release is needed for a specific package."""
    # Configure logging to stderr
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])

    package_prefix = os.environ.get("PACKAGE_PREFIX", "")
    target_pattern = os.environ.get("BAZEL_TARGET_PATTERN", "")

    if not package_prefix or not target_pattern:
        logger.error("Error: PACKAGE_PREFIX and BAZEL_TARGET_PATTERN must be set")
        sys.exit(1)

    logger.info("Checking if release needed for %s", package_prefix)
    logger.info("Target pattern: %s", target_pattern)

    workspace = get_workspace()
    repo = pygit2.Repository(workspace)

    outputs = compute_release_decision(repo, workspace, package_prefix, target_pattern)
    write_outputs(outputs)

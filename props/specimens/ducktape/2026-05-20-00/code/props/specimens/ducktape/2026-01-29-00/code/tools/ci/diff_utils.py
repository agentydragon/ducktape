"""Shared utilities for bazel-diff based CI tools.

Contains git utilities, infrastructure pattern detection, and bazel-diff execution.
Used by both ci_decide_lib.py and check_release_lib.py.
"""

from __future__ import annotations

import logging
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import pygit2

if TYPE_CHECKING:
    from tools.ci.github_actions import CIEnvironment

logger = logging.getLogger(__name__)

# Infrastructure patterns that affect all targets (caching may be invalid)
INFRA_PATTERNS = [
    r"^MODULE\.bazel$",
    r"^MODULE\.bazel\.lock$",
    r"^requirements_bazel\.txt$",
    r"^\.bazelrc$",
    r"^\.bazelversion$",
    r"^tools/bazel",
    r"^WORKSPACE",
]


def get_changed_files(repo: pygit2.Repository, base_commit: pygit2.Commit) -> set[str]:
    """Get set of files changed between base commit and HEAD."""
    head_commit = repo.head.peel(pygit2.Commit)
    return {delta.new_file.path for delta in repo.diff(base_commit, head_commit).deltas}


def has_infra_changes(changed_files: set[str]) -> bool:
    """Check if any changed files match infrastructure patterns.

    TODO: This conservatively treats any infra change as affecting all packages,
    which means e.g. updating an unrelated pip dependency triggers releases for
    all packages. Consider checking whether the wheel's transitive deps actually
    overlap with what changed.
    """
    compiled = [re.compile(p) for p in INFRA_PATTERNS]
    return any(r.match(f) for r in compiled for f in changed_files)


def get_ci_base_commit(repo: pygit2.Repository, env: CIEnvironment) -> pygit2.Commit | None:
    """Determine base commit for CI comparison (merge-base for PRs, HEAD~1 for pushes)."""
    if env.is_pull_request:
        if not env.base_ref:
            return None
        try:
            remote_ref = repo.references.get(f"refs/remotes/origin/{env.base_ref}")
            if remote_ref is None:
                return None
            base_commit = remote_ref.peel(pygit2.Commit)
            merge_base_oid = repo.merge_base(base_commit.id, repo.head.target)
            if merge_base_oid is None:
                return None
            logger.info("Pull request: comparing against merge-base %s", str(merge_base_oid)[:8])
            obj = repo.get(merge_base_oid)
            if not isinstance(obj, pygit2.Commit):
                return None
            return obj
        except (KeyError, pygit2.GitError):
            return None

    # Push event: compare against parent commit
    try:
        head_commit = repo.head.peel(pygit2.Commit)
        if head_commit.parents:
            parent = head_commit.parents[0]
            logger.info("Push: comparing against HEAD~1 (%s)", str(parent.id)[:8])
            return parent
    except (KeyError, pygit2.GitError):
        pass
    return None


BAZEL_DIFF_VERSION = "12.1.1"
BAZEL_DIFF_URL = f"https://github.com/Tinder/bazel-diff/releases/download/{BAZEL_DIFF_VERSION}/bazel-diff_deploy.jar"


def download_bazel_diff(dest: Path) -> None:
    """Download bazel-diff JAR if not already present."""
    if dest.exists():
        logger.info("bazel-diff already downloaded at %s", dest)
        return

    logger.info("Downloading bazel-diff v%s...", BAZEL_DIFF_VERSION)
    urllib.request.urlretrieve(BAZEL_DIFF_URL, dest)
    logger.info("Downloaded to %s", dest)


def checkout_commit(repo: pygit2.Repository, commit: pygit2.Commit) -> None:
    """Checkout a specific commit, updating the working directory."""
    repo.checkout_tree(commit, strategy=pygit2.GIT_CHECKOUT_FORCE)
    repo.set_head(commit.id)


def get_or_generate_hashes(
    repo: pygit2.Repository, jar_path: Path, workspace: Path, commit: pygit2.Commit, cache_dir: Path
) -> Path:
    """Get cached hashes or generate them for a commit.

    Returns path to the hash JSON file.
    """
    sha = str(commit.id)
    cached_path = cache_dir / f"{sha}.json"

    if cached_path.exists():
        logger.info("Using cached hashes for %s", sha[:8])
        return cached_path

    logger.info("Generating hashes for %s...", sha[:8])
    current_head = repo.head.peel(pygit2.Commit)
    checkout_commit(repo, commit)

    try:
        # Let stderr pass through to process stderr
        subprocess.run(
            ["java", "-jar", jar_path, "generate-hashes", "-w", workspace, "-b", "bazelisk", cached_path], check=True
        )
    finally:
        # Always restore to original HEAD
        checkout_commit(repo, current_head)

    return cached_path


class BazelDiffError(Exception):
    """Error running bazel-diff."""


def run_bazel_diff(
    repo: pygit2.Repository, jar_path: Path, workspace: Path, base_commit: pygit2.Commit, cache_dir: Path
) -> list[str]:
    """Run bazel-diff to compute impacted targets.

    Returns list of targets, or empty list if no changes.
    Raises BazelDiffError on failure.
    """
    head_commit = repo.head.peel(pygit2.Commit)
    cache_dir.mkdir(parents=True, exist_ok=True)

    base_json = get_or_generate_hashes(repo, jar_path, workspace, base_commit, cache_dir)
    head_json = get_or_generate_hashes(repo, jar_path, workspace, head_commit, cache_dir)

    # Compute impacted targets (stderr passes through to process stderr)
    logger.info("Computing impacted targets...")
    try:
        result = subprocess.run(
            ["java", "-jar", jar_path, "get-impacted-targets", "-sh", base_json, "-fh", head_json],
            check=True,
            capture_output=False,
            stdout=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise BazelDiffError(f"bazel-diff get-impacted-targets failed with exit code {e.returncode}") from e

    return [t for t in result.stdout.strip().split("\n") if t]

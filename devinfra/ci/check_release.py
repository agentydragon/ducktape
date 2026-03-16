#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "pygit2>=1.14"]
# ///
"""Check if a release is needed for a specific package.

Compares against the floating latest release tag using bazel-diff to determine
if any of the package's build targets have been affected.

Usage:
    PACKAGE_PREFIX=ducktape BAZEL_TARGETS="//:wheel" \
        LATEST_RELEASE_TAG=ducktape-latest \
        uv run devinfra/ci/check_release.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Add repo root to path for devinfra.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from typing import Annotated

import pygit2
from pydantic import BaseModel, BeforeValidator

from devinfra.ci.diff_utils import download_bazel_diff, get_changed_files, has_infra_changes, run_bazel_diff
from devinfra.ci.github_actions import CIEnvironment
from util.bazel.workspace import BazelLabel
from util.env import get_required_env

logger = logging.getLogger(__name__)


class ReleaseEnvironment(BaseModel):
    """Environment for release checks."""

    ci: CIEnvironment
    package_prefix: str
    bazel_targets: list[Annotated[BazelLabel, BeforeValidator(BazelLabel.parse)]]
    latest_release_tag: str

    @classmethod
    def from_env(cls) -> ReleaseEnvironment:
        """Load release environment from os.environ."""
        return cls(
            ci=CIEnvironment.from_env(),
            package_prefix=get_required_env("PACKAGE_PREFIX"),
            bazel_targets=get_required_env("BAZEL_TARGETS").split(),
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

    Checks if any of the package's build targets are in the affected targets list.
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
    affected = run_bazel_diff(repo, jar_path, env.ci.workspace, base_commit, cache_dir)
    logger.info("Found %d affected targets total", len(affected))

    hit = set(env.bazel_targets) & affected
    for t in env.bazel_targets:
        logger.info("Target %s %s", t, "changed" if t in hit else "not affected")
    return bool(hit)


def main() -> None:
    """Main entry point - check if release is needed for a specific package."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])

    env = ReleaseEnvironment.from_env()

    logger.info("Checking if release needed for %s", env.package_prefix)
    logger.info("Targets: %s", " ".join(str(t) for t in env.bazel_targets))
    logger.info("Latest release tag: %s", env.latest_release_tag)

    repo = pygit2.Repository(env.ci.workspace)
    release_needed = compute_release_decision(env, repo)
    env.ci.write_outputs({"release_needed": release_needed})


if __name__ == "__main__":
    main()

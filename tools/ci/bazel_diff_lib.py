"""Compute affected Bazel targets using bazel-diff.

Supports two modes:
1. CI mode (default): Compare against previous commit or merge-base for PRs
2. Release mode: Compare against last release tag for a specific package

This module provides the implementation logic. See bazel_diff.py for the CLI entry point.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import urllib.request
from pathlib import Path

import pygit2
from pydantic import BaseModel, Field, computed_field

from fmt_util import format_limited_list
from tools.ci.bazel_query import filter_to_rules, run_query
from tools.ci.diff_utils import get_changed_files, get_ci_base_commit, has_infra_changes, run_bazel_diff
from tools.ci.github_actions import bool_output, write_outputs

logger = logging.getLogger(__name__)

BAZEL_DIFF_VERSION = "12.1.1"
BAZEL_DIFF_URL = f"https://github.com/Tinder/bazel-diff/releases/download/{BAZEL_DIFF_VERSION}/bazel-diff_deploy.jar"

# Path patterns for conditional job triggers (CI mode only)
PATH_PATTERNS = {
    "has_props": "//props/...",
    "has_editor_agent": "//editor_agent/...",
    "has_agent_server": "//agent_server/...",
    "has_finance": "//finance/...",
    "has_props_frontend": "//props/frontend/...",
}

# Workflow file patterns that force certain outputs to be true
# (workflow file changes should trigger the corresponding workflow)
WORKFLOW_TRIGGERS = {
    r"^\.github/workflows/props-e2e-test\.yml$": ["has_props"],
    r"^\.github/workflows/editor-e2e-test\.yml$": ["has_editor_agent"],
    r"^\.github/workflows/agent-server-e2e-test\.yml$": ["has_agent_server"],
}


class AffectedTargets(BaseModel):
    """Result of computing affected targets."""

    targets: list[str] = Field(default_factory=list)
    has_changes: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def targets_str(self) -> str:
        """Return targets as space-separated string for output."""
        return " ".join(self.targets) if self.targets else ""


def output_results(affected: AffectedTargets, intersections: dict[str, bool]) -> None:
    """Output all results to GitHub Actions output."""
    outputs = {"targets": affected.targets_str, "has_changes": bool_output(affected.has_changes)}
    for var, val in intersections.items():
        outputs[var] = bool_output(val)
    write_outputs(outputs)


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


def check_intersection(targets: list[str], pattern: str) -> bool:
    """Check if affected targets intersect with a pattern using bazel query."""
    if not targets:
        return False

    # Full build (//...) checks pattern directly; otherwise compute set intersection
    query = pattern if targets == ["//..."] else f"set({' '.join(targets)}) intersect {pattern}"
    result = run_query(query)
    return bool(result.stdout.strip())


def check_workflow_triggers(changed_files: set[str]) -> dict[str, bool]:
    """Check if any changed workflow files force certain outputs to be true."""
    triggers: dict[str, bool] = {}
    compiled = [(re.compile(p), vars) for p, vars in WORKFLOW_TRIGGERS.items()]

    for file in changed_files:
        for pattern, var_names in compiled:
            if pattern.match(file):
                logger.info("Workflow file %s triggers: %s", file, var_names)
                for var in var_names:
                    triggers[var] = True

    return triggers


def compute_intersections(
    targets: list[str], has_changes: bool, changed_files: set[str] | None = None
) -> dict[str, bool]:
    """Compute intersection flags for all path patterns.

    Checks both Bazel target intersections and workflow file triggers.
    Workflow file changes can trigger jobs even if no Bazel targets changed.
    """
    result = dict.fromkeys(PATH_PATTERNS, False)

    # Check workflow file triggers first (these apply even without Bazel changes)
    if changed_files:
        workflow_triggers = check_workflow_triggers(changed_files)
        for var, val in workflow_triggers.items():
            if val:
                result[var] = True

    # If no Bazel changes, only workflow triggers apply
    if not has_changes:
        return result

    # Check Bazel target intersections
    logger.info("Computing path intersections...")
    for var_name, pattern in PATH_PATTERNS.items():
        if check_intersection(targets, pattern):
            result[var_name] = True

    return result


FULL_BUILD = AffectedTargets(targets=["//..."], has_changes=True)
NO_CHANGES = AffectedTargets(targets=[], has_changes=False)


def compute_affected_for_pr(repo: pygit2.Repository, workspace: Path) -> tuple[AffectedTargets, set[str]]:
    """Compute affected targets for a pull request.

    Returns tuple of (affected, changed_files).
    """
    jar_path = Path("/tmp/bazel-diff.jar")
    download_bazel_diff(jar_path)

    base_commit = get_ci_base_commit(repo)
    if not base_commit:
        logger.info("No base commit (new branch or initial commit), running all targets")
        return FULL_BUILD, set()

    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files: %s", format_limited_list(sorted(changed_files), 20))

    if has_infra_changes(changed_files):
        logger.info("Infrastructure change detected, running all targets")
        return FULL_BUILD, changed_files

    targets = run_bazel_diff(repo, jar_path, workspace, base_commit)
    if targets is None:
        return FULL_BUILD, changed_files

    if not targets:
        logger.info("No Bazel targets affected")
        return NO_CHANGES, changed_files

    # Filter source files - bazel-diff returns labels like //:foo.py that aren't buildable
    raw_count = len(targets)
    targets = filter_to_rules(targets)
    if len(targets) < raw_count:
        logger.info("Filtered %d source files from %d bazel-diff targets", raw_count - len(targets), raw_count)

    logger.info("Found %d affected targets: %s", len(targets), format_limited_list(targets, 20))
    return AffectedTargets(targets=targets, has_changes=True), changed_files


def run_ci_mode() -> None:
    """Run in CI mode: compute affected targets for general CI jobs."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())

    repo = pygit2.Repository(workspace)

    # Full build on main/devel branches (only use diffs for PRs)
    if event_name != "pull_request":
        logger.info("Push to %s branch, running full build", ref_name)
        affected = FULL_BUILD
        changed_files: set[str] = set()
    else:
        affected, changed_files = compute_affected_for_pr(repo, workspace)

    intersections = compute_intersections(affected.targets, affected.has_changes, changed_files)
    output_results(affected, intersections)


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

    if not check_intersection(targets, target_pattern):
        return _release_output(False, base_sha, f"no targets matching {target_pattern} changed since last release")

    query = f"set({' '.join(targets)}) intersect {target_pattern}"
    result = run_query(query)
    matching = [t for t in result.stdout.strip().split("\n") if t]
    logger.info("Found %d matching targets:", len(matching))
    for t in matching[:10]:
        logger.info("  %s", t)
    if len(matching) > 10:
        logger.info("  ... and %d more", len(matching) - 10)

    return _release_output(True, base_sha, f"{len(matching)} targets matching {target_pattern} changed")


def run_release_mode() -> None:
    """Run in release mode: check if release is needed for a specific package."""
    package_prefix = os.environ.get("PACKAGE_PREFIX", "")
    target_pattern = os.environ.get("BAZEL_TARGET_PATTERN", "")

    if not package_prefix or not target_pattern:
        logger.error("Error: PACKAGE_PREFIX and BAZEL_TARGET_PATTERN must be set")
        sys.exit(1)

    logger.info("Checking if release needed for %s", package_prefix)
    logger.info("Target pattern: %s", target_pattern)

    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
    repo = pygit2.Repository(workspace)

    outputs = compute_release_decision(repo, workspace, package_prefix, target_pattern)
    write_outputs(outputs)


def main() -> None:
    """Main entry point."""
    # Configure logging to stderr
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])

    if os.environ.get("RELEASE_MODE"):
        run_release_mode()
    else:
        run_ci_mode()

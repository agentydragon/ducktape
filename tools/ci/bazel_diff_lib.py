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
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Collection
from pathlib import Path

import pygit2
from pydantic import BaseModel, Field, computed_field

from tools.ci.bazel_query import run_query

logger = logging.getLogger(__name__)

BAZEL_DIFF_VERSION = "12.1.1"
BAZEL_DIFF_URL = f"https://github.com/Tinder/bazel-diff/releases/download/{BAZEL_DIFF_VERSION}/bazel-diff_deploy.jar"

# Infrastructure patterns that require full build (changes affect all targets)
INFRA_PATTERNS = [
    r"^MODULE\.bazel$",
    r"^MODULE\.bazel\.lock$",
    r"^requirements_bazel\.txt$",
    r"^\.bazelrc$",
    r"^\.bazelversion$",
    r"^tools/bazel",  # More specific than "tools/" to avoid triggering on tools/ci changes
    r"^WORKSPACE",
]

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


def bool_str(value: bool) -> str:
    """Convert bool to GitHub Actions output string."""
    return "true" if value else "false"


def write_outputs(outputs: dict[str, str]) -> None:
    """Write all key-value pairs to GitHub Actions output.

    Opens with 'w' to ensure a clean write (no stale entries from prior runs).
    """
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with Path(output_file).open("w") as f:
            f.writelines(f"{key}={value}\n" for key, value in outputs.items())
    for key, value in outputs.items():
        logger.info("%s=%s", key, value)


def output_results(affected: AffectedTargets, intersections: dict[str, bool]) -> None:
    """Output all results to GitHub Actions output."""
    outputs = {"targets": affected.targets_str, "has_changes": bool_str(affected.has_changes)}
    for var, val in intersections.items():
        outputs[var] = bool_str(val)
    write_outputs(outputs)


def print_truncated(label: str, items: Collection[str], limit: int = 20) -> None:
    """Print a collection on one line, truncating if over limit."""
    items_list = list(items)
    shown = items_list[:limit]
    suffix = f" ... and {len(items_list) - limit} more" if len(items_list) > limit else ""
    logger.info("%s: %s%s", label, ", ".join(shown), suffix)


def download_bazel_diff(dest: Path) -> bool:
    """Download bazel-diff JAR.

    Returns True on success, False on failure.
    """
    if dest.exists():
        logger.info("bazel-diff already downloaded at %s", dest)
        return True

    logger.info("Downloading bazel-diff v%s...", BAZEL_DIFF_VERSION)
    try:
        urllib.request.urlretrieve(BAZEL_DIFF_URL, dest)
        logger.info("Downloaded to %s", dest)
        return True
    except Exception as e:
        logger.error("Failed to download bazel-diff: %s", e)
        return False


def get_changed_files(repo: pygit2.Repository, base_commit: pygit2.Commit) -> set[str]:
    """Get set of files changed between base_commit and HEAD using pygit2."""
    head_commit = repo.head.peel(pygit2.Commit)
    diff = repo.diff(base_commit, head_commit)
    return {delta.new_file.path for delta in diff.deltas}


def has_infra_changes(changed_files: set[str]) -> bool:
    """Check if any changed files match infrastructure patterns."""
    compiled = [re.compile(p) for p in INFRA_PATTERNS]
    return any(r.match(f) for r in compiled for f in changed_files)


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


def get_ci_base_commit(repo: pygit2.Repository) -> pygit2.Commit | None:
    """Determine base commit for CI comparison (merge-base for PRs, HEAD~1 for pushes)."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")

    if event_name == "pull_request":
        base_ref = os.environ.get("GITHUB_BASE_REF", "")
        if not base_ref:
            return None
        try:
            remote_ref = repo.references.get(f"refs/remotes/origin/{base_ref}")
            if remote_ref is None:
                return None
            base_commit = remote_ref.peel(pygit2.Commit)
            merge_base_oid = repo.merge_base(base_commit.id, repo.head.target)
            if merge_base_oid is None:
                return None
            obj = repo.get(merge_base_oid)
            if not isinstance(obj, pygit2.Commit):
                return None
            logger.info("Pull request: comparing against merge-base %s", str(merge_base_oid)[:8])
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


def checkout_commit(repo: pygit2.Repository, commit: pygit2.Commit) -> None:
    """Checkout a specific commit, updating the working directory."""
    repo.checkout_tree(commit, strategy=pygit2.GIT_CHECKOUT_FORCE)
    repo.set_head(commit.id)


def run_bazel_diff(
    jar_path: Path, workspace: Path, repo: pygit2.Repository, base_commit: pygit2.Commit
) -> list[str] | None:
    """Run bazel-diff to compute impacted targets.

    Returns:
        List of impacted targets, or None on failure (triggers full build fallback)
    """
    current_commit = repo.head.peel(pygit2.Commit)
    current_sha = str(current_commit.id)
    base_sha = str(base_commit.id)

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        base_json = tmpdir / "base.json"
        head_json = tmpdir / "head.json"
        targets_file = tmpdir / "targets.txt"

        # Generate hashes for base commit
        logger.info("Generating hashes for base commit %s...", base_sha[:8])
        checkout_commit(repo, base_commit)

        try:
            subprocess.run(
                ["java", "-jar", jar_path, "generate-hashes", "-w", workspace, "-b", "bazelisk", base_json],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            logger.warning("Base hash generation failed, falling back to full build")
            checkout_commit(repo, current_commit)
            return None

        # Generate hashes for head commit
        logger.info("Generating hashes for head commit %s...", current_sha[:8])
        checkout_commit(repo, current_commit)

        try:
            subprocess.run(
                ["java", "-jar", jar_path, "generate-hashes", "-w", workspace, "-b", "bazelisk", head_json],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            logger.warning("Head hash generation failed, falling back to full build")
            return None

        # Compute impacted targets
        logger.info("Computing impacted targets...")
        try:
            subprocess.run(
                [
                    "java",
                    "-jar",
                    jar_path,
                    "get-impacted-targets",
                    "-sh",
                    base_json,
                    "-fh",
                    head_json,
                    "-o",
                    targets_file,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            logger.warning("Target diff failed, falling back to full build")
            return None

        if not targets_file.exists() or targets_file.stat().st_size == 0:
            return []

        return [t for t in targets_file.read_text().strip().split("\n") if t]


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
    if not download_bazel_diff(jar_path):
        logger.warning("Failed to download bazel-diff, running full build")
        return FULL_BUILD, set()

    base_commit = get_ci_base_commit(repo)
    if not base_commit:
        logger.info("No base commit (new branch or initial commit), running all targets")
        return FULL_BUILD, set()

    changed_files = get_changed_files(repo, base_commit)
    print_truncated("Changed files", changed_files)

    if has_infra_changes(changed_files):
        logger.info("Infrastructure change detected, running all targets")
        return FULL_BUILD, changed_files

    targets = run_bazel_diff(jar_path, workspace, repo, base_commit)
    if targets is None:
        return FULL_BUILD, changed_files

    if not targets:
        logger.info("No Bazel targets affected")
        return NO_CHANGES, changed_files

    print_truncated(f"Found {len(targets)} affected targets", targets)
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

    # Find the last release commit
    base_commit = get_last_release_commit(repo, package_prefix)

    if not base_commit:
        write_outputs({"release_needed": "true", "base_sha": "", "reason": "first release (no previous release found)"})
        return

    base_sha = str(base_commit.id)
    logger.info("Last release commit: %s", base_sha)

    # Check for infrastructure changes first
    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files since last release: %d", len(changed_files))

    if has_infra_changes(changed_files):
        write_outputs(
            {
                "release_needed": "true",
                "base_sha": base_sha,
                "reason": "infrastructure files changed, assuming release needed",
            }
        )
        return

    # Download bazel-diff
    jar_path = Path("/tmp/bazel-diff.jar")
    if not download_bazel_diff(jar_path):
        write_outputs(
            {
                "release_needed": "true",
                "base_sha": base_sha,
                "reason": "failed to download bazel-diff, assuming release needed",
            }
        )
        return

    # Run bazel-diff
    targets = run_bazel_diff(jar_path, workspace, repo, base_commit)

    if targets is None:
        write_outputs(
            {"release_needed": "true", "base_sha": base_sha, "reason": "bazel-diff failed, assuming release needed"}
        )
        return

    if not targets:
        write_outputs(
            {"release_needed": "false", "base_sha": base_sha, "reason": "no Bazel targets affected since last release"}
        )
        return

    logger.info("Found %d affected targets total", len(targets))

    # Check if any targets match the pattern
    if check_intersection(targets, target_pattern):
        query = f"set({' '.join(targets)}) intersect {target_pattern}"
        result = run_query(query)
        matching = [t for t in result.stdout.strip().split("\n") if t]
        logger.info("Found %d matching targets:", len(matching))
        for t in matching[:10]:
            logger.info("  %s", t)
        if len(matching) > 10:
            logger.info("  ... and %d more", len(matching) - 10)
        write_outputs(
            {
                "release_needed": "true",
                "base_sha": base_sha,
                "reason": f"{len(matching)} targets matching {target_pattern} changed",
            }
        )
    else:
        write_outputs(
            {
                "release_needed": "false",
                "base_sha": base_sha,
                "reason": f"no targets matching {target_pattern} changed since last release",
            }
        )


def main() -> None:
    """Main entry point."""
    # Configure logging to stderr
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])

    if os.environ.get("RELEASE_MODE"):
        run_release_mode()
    else:
        run_ci_mode()

#!/usr/bin/env python3
"""Compute affected Bazel targets using bazel-diff.

Supports two modes:
1. CI mode (default): Compare against previous commit or merge-base for PRs
2. Release mode: Compare against last release tag for a specific package

Usage:
    # CI mode
    python compute_affected_targets.py

    # Release mode
    RELEASE_MODE=1 PACKAGE_PREFIX=ducktape BAZEL_TARGET_PATTERN="//..." python compute_affected_targets.py

Outputs to $GITHUB_OUTPUT:
    targets: space-separated list of affected targets, or "//..." for full build
    has_changes: "true" or "false"

    # CI mode only:
    has_props: "true" if //props/... targets are affected
    has_editor_agent: "true" if //editor_agent/... targets are affected
    has_agent_server: "true" if //agent_server/... targets are affected
    has_finance: "true" if //finance/... targets are affected
    has_props_frontend: "true" if //props/frontend/... targets are affected

    # Release mode only:
    release_needed: "true" or "false"
    base_sha: The last release commit SHA (empty if first release)
    reason: Human-readable explanation
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add repo root to path for tools.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import os
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass

from tools.ci.bazel_query import run_query_with_file

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


@dataclass
class AffectedTargets:
    """Result of computing affected targets."""

    targets: str  # Space-separated targets or "//..."
    has_changes: bool


def run_cmd(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command, optionally checking return code."""
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def bool_str(value: bool) -> str:
    """Convert bool to GitHub Actions output string."""
    return "true" if value else "false"


def output(key: str, value: str) -> None:
    """Write a key-value pair to GitHub Actions output."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with Path(output_file).open("a") as f:
            f.write(f"{key}={value}\n")
    print(f"{key}={value}")


def print_truncated(label: str, items: list[str], limit: int = 20) -> None:
    """Print a list, truncating if over limit."""
    print(f"{label}:")
    for item in items[:limit]:
        print(f"  {item}")
    if len(items) > limit:
        print(f"  ... and {len(items) - limit} more")


def download_bazel_diff(dest: Path) -> bool:
    """Download bazel-diff JAR.

    Returns True on success, False on failure.
    """
    if dest.exists():
        print(f"bazel-diff already downloaded at {dest}")
        return True

    print(f"Downloading bazel-diff v{BAZEL_DIFF_VERSION}...")
    try:
        urllib.request.urlretrieve(BAZEL_DIFF_URL, dest)
        print(f"Downloaded to {dest}")
        return True
    except Exception as e:
        print(f"Failed to download bazel-diff: {e}", file=sys.stderr)
        return False


def get_changed_files(base_sha: str) -> list[str]:
    """Get list of files changed between base_sha and HEAD."""
    result = run_cmd(["git", "diff", "--name-only", f"{base_sha}...HEAD"], check=False)
    if result.returncode != 0:
        return []
    return [f for f in result.stdout.strip().split("\n") if f]


def has_infra_changes(changed_files: list[str]) -> bool:
    """Check if any changed files match infrastructure patterns."""
    compiled = [re.compile(p) for p in INFRA_PATTERNS]
    return any(r.match(f) for r in compiled for f in changed_files)


def get_last_release_commit(package_prefix: str) -> str | None:
    """Find the commit SHA of the last release for a package."""
    result = run_cmd(["git", "tag", "-l", f"{package_prefix}-*", "--sort=-creatordate"], check=False)

    if result.returncode != 0:
        print(f"Failed to list tags: {result.stderr}", file=sys.stderr)
        return None

    tags = result.stdout.strip().split("\n")
    tags = [t for t in tags if t and "latest" not in t]

    if not tags:
        return None

    latest_tag = tags[0]
    print(f"Found last release tag: {latest_tag}")

    result = run_cmd(["git", "rev-parse", f"{latest_tag}^{{commit}}"], check=False)

    if result.returncode != 0:
        print(f"Failed to resolve tag {latest_tag}: {result.stderr}", file=sys.stderr)
        return None

    return result.stdout.strip()


def get_ci_base_sha() -> str | None:
    """Determine base SHA for CI comparison (merge-base for PRs, HEAD~1 for pushes)."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")

    if event_name == "pull_request":
        base_ref = os.environ.get("GITHUB_BASE_REF", "")
        result = run_cmd(["git", "merge-base", f"origin/{base_ref}", "HEAD"], capture=True)
        sha = result.stdout.strip()
        print(f"Pull request: comparing against merge-base {sha}")
        return sha
    result = run_cmd(["git", "rev-parse", "HEAD~1"], check=False, capture=True)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    print(f"Push: comparing against HEAD~1 ({sha})")
    return sha


def run_bazel_diff(jar_path: Path, workspace: str, base_sha: str) -> list[str] | None:
    """Run bazel-diff to compute impacted targets.

    Returns:
        List of impacted targets, or None on failure (triggers full build fallback)
    """
    current_sha = run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()

    with tempfile.TemporaryDirectory() as tmpdir:
        base_json = Path(tmpdir) / "base.json"
        head_json = Path(tmpdir) / "head.json"
        targets_file = Path(tmpdir) / "targets.txt"

        # Generate hashes for base commit
        print(f"Generating hashes for base commit {base_sha[:8]}...")
        run_cmd(["git", "checkout", "--quiet", base_sha])

        result = run_cmd(
            ["java", "-jar", str(jar_path), "generate-hashes", "-w", workspace, "-b", "bazelisk", str(base_json)],
            check=False,
        )
        if result.returncode != 0:
            print("Base hash generation failed, falling back to full build")
            run_cmd(["git", "checkout", "--quiet", current_sha])
            return None

        # Generate hashes for head commit
        print(f"Generating hashes for head commit {current_sha[:8]}...")
        run_cmd(["git", "checkout", "--quiet", current_sha])

        result = run_cmd(
            ["java", "-jar", str(jar_path), "generate-hashes", "-w", workspace, "-b", "bazelisk", str(head_json)],
            check=False,
        )
        if result.returncode != 0:
            print("Head hash generation failed, falling back to full build")
            return None

        # Compute impacted targets
        print("Computing impacted targets...")
        result = run_cmd(
            [
                "java",
                "-jar",
                str(jar_path),
                "get-impacted-targets",
                "-sh",
                str(base_json),
                "-fh",
                str(head_json),
                "-o",
                str(targets_file),
            ],
            check=False,
        )
        if result.returncode != 0:
            print("Target diff failed, falling back to full build")
            return None

        if not targets_file.exists() or targets_file.stat().st_size == 0:
            return []

        return [t for t in targets_file.read_text().strip().split("\n") if t]


def check_intersection(targets: str, pattern: str) -> bool:
    """Check if affected targets intersect with a pattern using bazel query.

    Uses --query_file to avoid "Argument list too long" errors with large target sets.
    """
    if not targets:
        return False

    # Full build checks pattern directly; otherwise compute set intersection
    query = pattern if targets == "//..." else f"set({targets}) intersect {pattern}"
    result = run_query_with_file(query)
    return bool(result.stdout.strip())


def check_workflow_triggers(changed_files: list[str]) -> dict[str, bool]:
    """Check if any changed workflow files force certain outputs to be true."""
    triggers: dict[str, bool] = {}
    compiled = [(re.compile(p), vars) for p, vars in WORKFLOW_TRIGGERS.items()]

    for file in changed_files:
        for pattern, var_names in compiled:
            if pattern.match(file):
                print(f"Workflow file {file} triggers: {var_names}")
                for var in var_names:
                    triggers[var] = True

    return triggers


def compute_intersections(targets: str, has_changes: bool, changed_files: list[str] | None = None) -> dict[str, bool]:
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
    print("Computing path intersections...")
    for var_name, pattern in PATH_PATTERNS.items():
        if check_intersection(targets, pattern):
            result[var_name] = True

    return result


def run_ci_mode() -> None:
    """Run in CI mode: compute affected targets for general CI jobs."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    workspace = os.environ.get("GITHUB_WORKSPACE") or str(Path.cwd())

    changed_files: list[str] = []

    # Full build on main/devel branches (only use diffs for PRs)
    if event_name != "pull_request":
        print(f"Push to {ref_name} branch, running full build")
        affected = AffectedTargets(targets="//...", has_changes=True)
    else:
        jar_path = Path("/tmp/bazel-diff.jar")
        if not download_bazel_diff(jar_path):
            print("Failed to download bazel-diff, running full build")
            affected = AffectedTargets(targets="//...", has_changes=True)
        else:
            base_sha = get_ci_base_sha()
            if not base_sha:
                print("No base SHA (new branch or initial commit), running all targets")
                affected = AffectedTargets(targets="//...", has_changes=True)
            else:
                changed_files = get_changed_files(base_sha)
                print_truncated("Changed files", changed_files)

                if has_infra_changes(changed_files):
                    print("Infrastructure change detected, running all targets")
                    affected = AffectedTargets(targets="//...", has_changes=True)
                else:
                    targets = run_bazel_diff(jar_path, workspace, base_sha)
                    if targets is None:
                        affected = AffectedTargets(targets="//...", has_changes=True)
                    elif not targets:
                        print("No Bazel targets affected")
                        affected = AffectedTargets(targets="", has_changes=False)
                    else:
                        print_truncated(f"Found {len(targets)} affected targets", targets)
                        affected = AffectedTargets(targets=" ".join(targets), has_changes=True)

    # Compute intersections for conditional jobs (also checks workflow file triggers)
    intersections = compute_intersections(affected.targets, affected.has_changes, changed_files)

    # Output results
    output("targets", affected.targets)
    output("has_changes", bool_str(affected.has_changes))
    for var, val in intersections.items():
        output(var, bool_str(val))


def run_release_mode() -> None:
    """Run in release mode: check if release is needed for a specific package."""
    package_prefix = os.environ.get("PACKAGE_PREFIX", "")
    target_pattern = os.environ.get("BAZEL_TARGET_PATTERN", "")

    if not package_prefix or not target_pattern:
        print("Error: PACKAGE_PREFIX and BAZEL_TARGET_PATTERN must be set")
        sys.exit(1)

    print(f"Checking if release needed for {package_prefix}")
    print(f"Target pattern: {target_pattern}")

    workspace = os.environ.get("GITHUB_WORKSPACE") or str(Path.cwd())

    # Find the last release commit
    base_sha = get_last_release_commit(package_prefix)

    if not base_sha:
        output("release_needed", "true")
        output("base_sha", "")
        output("reason", "first release (no previous release found)")
        return

    print(f"Last release commit: {base_sha}")
    output("base_sha", base_sha)

    # Check for infrastructure changes first
    changed_files = get_changed_files(base_sha)
    print(f"Changed files since last release: {len(changed_files)}")

    if has_infra_changes(changed_files):
        output("release_needed", "true")
        output("reason", "infrastructure files changed, assuming release needed")
        return

    # Download bazel-diff
    jar_path = Path("/tmp/bazel-diff.jar")
    if not download_bazel_diff(jar_path):
        output("release_needed", "true")
        output("reason", "failed to download bazel-diff, assuming release needed")
        return

    # Run bazel-diff
    targets = run_bazel_diff(jar_path, workspace, base_sha)

    if targets is None:
        output("release_needed", "true")
        output("reason", "bazel-diff failed, assuming release needed")
        return

    if not targets:
        output("release_needed", "false")
        output("reason", "no Bazel targets affected since last release")
        return

    print(f"Found {len(targets)} affected targets total")

    # Check if any targets match the pattern
    query = f"set({' '.join(targets)}) intersect {target_pattern}"
    result = run_query_with_file(query)

    if result.stdout.strip():
        matching = result.stdout.strip().split("\n")
        print(f"Found {len(matching)} matching targets:")
        for t in matching[:10]:
            print(f"  {t}")
        if len(matching) > 10:
            print(f"  ... and {len(matching) - 10} more")
        output("release_needed", "true")
        output("reason", f"{len(matching)} targets matching {target_pattern} changed")
    else:
        output("release_needed", "false")
        output("reason", f"no targets matching {target_pattern} changed since last release")


def main() -> None:
    if os.environ.get("RELEASE_MODE"):
        run_release_mode()
    else:
        run_ci_mode()


if __name__ == "__main__":
    main()

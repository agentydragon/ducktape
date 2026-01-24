#!/usr/bin/env python3
"""CI decision engine - computes affected targets and workflows to run.

Reads workflow definitions from workflows.yaml and uses bazel-diff to compute
exactly which Bazel targets are affected. Outputs a JSON list of workflows
to trigger instead of individual boolean flags.

Outputs to $GITHUB_OUTPUT:
    targets: space-separated list of affected Bazel targets (or "//..." on infra change)
    workflows: JSON array of workflow names to run
    infra_changed: "true" if infrastructure files changed (MODULE.bazel, etc.)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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


@dataclass
class WorkflowConfig:
    """Configuration for a single workflow."""

    name: str
    bazel_pattern: str | None = None
    path_pattern: str | None = None
    always: bool = False
    receives_targets: bool = False


@dataclass
class CIDecision:
    """Result of CI decision computation."""

    targets: str  # Space-separated Bazel targets or "//..."
    workflows: list[str] = field(default_factory=list)
    infra_changed: bool = False


def run_cmd(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command, optionally checking return code."""
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def output(key: str, value: str) -> None:
    """Write a key-value pair to GitHub Actions output."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with Path(output_file).open("a") as f:
            f.write(f"{key}={value}\n")
    print(f"{key}={value}")


def bool_str(value: bool) -> str:
    """Convert bool to GitHub Actions output string."""
    return "true" if value else "false"


def print_truncated(label: str, items: list[str], limit: int = 20) -> None:
    """Print a list, truncating if over limit."""
    print(f"{label}:")
    for item in items[:limit]:
        print(f"  {item}")
    if len(items) > limit:
        print(f"  ... and {len(items) - limit} more")


def load_workflows(manifest_path: Path) -> dict[str, WorkflowConfig]:
    """Load workflow definitions from YAML manifest."""
    with manifest_path.open() as f:
        data = yaml.safe_load(f)

    workflows = {}
    for name, config in data.items():
        workflows[name] = WorkflowConfig(
            name=name,
            bazel_pattern=config.get("bazel_pattern"),
            path_pattern=config.get("path_pattern"),
            always=config.get("always", False),
            receives_targets=config.get("receives_targets", False),
        )
    return workflows


def get_base_sha() -> str | None:
    """Determine base SHA for comparison (merge-base for PRs, HEAD~1 for pushes)."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")

    if event_name == "pull_request":
        base_ref = os.environ.get("GITHUB_BASE_REF", "")
        if not base_ref:
            return None
        result = run_cmd(["git", "merge-base", f"origin/{base_ref}", "HEAD"], check=False)
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        print(f"Pull request: comparing against merge-base {sha}")
        return sha

    # Push event: compare against previous commit
    result = run_cmd(["git", "rev-parse", "HEAD~1"], check=False, capture=True)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    print(f"Push: comparing against HEAD~1 ({sha})")
    return sha


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


def run_bazel_diff(jar_path: Path, workspace: str, base_sha: str) -> list[str] | None:
    """Run bazel-diff to compute impacted targets.

    Returns list of targets, empty list if no changes, or None on failure.
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
            print("Base hash generation failed")
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
            print("Head hash generation failed")
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
            print("Target diff failed")
            return None

        if not targets_file.exists() or targets_file.stat().st_size == 0:
            return []

        return [t for t in targets_file.read_text().strip().split("\n") if t]


def check_bazel_intersection(targets: str, pattern: str) -> bool:
    """Check if affected targets intersect with a Bazel pattern."""
    if not targets:
        return False

    # Full build ("//...") always intersects with any pattern
    if targets == "//...":
        return True

    query = f"set({targets}) intersect {pattern}"
    result = run_cmd(["bazelisk", "query", query], check=False)
    return bool(result.stdout.strip())


def get_workflows_from_file_changes(changed_files: list[str], workflows: dict[str, WorkflowConfig]) -> set[str]:
    """Detect workflows triggered by their own workflow file changing."""
    triggered = set()
    for f in changed_files:
        if m := re.match(r"^\.github/workflows/([^/]+)\.yml$", f):
            workflow_name = m.group(1)
            if workflow_name in workflows:
                print(f"Workflow file changed: {f} -> triggers {workflow_name}")
                triggered.add(workflow_name)
    return triggered


def compute_triggered_workflows(
    workflows: dict[str, WorkflowConfig], targets: str, changed_files: list[str], has_bazel_changes: bool
) -> list[str]:
    """Determine which workflows should run based on changes."""
    triggered: set[str] = set()

    # 1. Always-run workflows
    for name, config in workflows.items():
        if config.always:
            triggered.add(name)

    # 2. Workflow file changes trigger their workflow
    triggered.update(get_workflows_from_file_changes(changed_files, workflows))

    # 3. Path-pattern workflows
    for name, config in workflows.items():
        if config.path_pattern:
            pattern = re.compile(config.path_pattern)
            if any(pattern.match(f) for f in changed_files):
                print(f"Path pattern '{config.path_pattern}' matched -> triggers {name}")
                triggered.add(name)

    # 4. Bazel-pattern workflows (only if we have Bazel changes)
    if has_bazel_changes:
        for name, config in workflows.items():
            if config.bazel_pattern and check_bazel_intersection(targets, config.bazel_pattern):
                triggered.add(name)

    return sorted(triggered)


def main() -> None:
    # Load workflow manifest
    script_dir = Path(__file__).parent
    manifest_path = script_dir / "workflows.yaml"
    if not manifest_path.exists():
        print(f"Error: {manifest_path} not found", file=sys.stderr)
        sys.exit(1)

    workflows = load_workflows(manifest_path)
    print(f"Loaded {len(workflows)} workflow definitions")

    workspace = os.environ.get("GITHUB_WORKSPACE") or str(Path.cwd())

    # Get base SHA for comparison
    base_sha = get_base_sha()
    if not base_sha:
        print("No base SHA (new branch or initial commit), triggering all workflows")
        decision = CIDecision(targets="//...", workflows=sorted(workflows.keys()), infra_changed=True)
    else:
        # Get changed files
        changed_files = get_changed_files(base_sha)
        print_truncated("Changed files", changed_files)

        # Check for infrastructure changes
        infra_changed = has_infra_changes(changed_files)
        if infra_changed:
            print("Infrastructure change detected")

        # Run bazel-diff (JAR path from env, downloaded by CI workflow)
        jar_path_str = os.environ.get("BAZEL_DIFF_JAR")
        if not jar_path_str:
            print("Error: BAZEL_DIFF_JAR environment variable not set", file=sys.stderr)
            sys.exit(1)

        jar_path = Path(jar_path_str)
        if not jar_path.exists():
            print(f"Error: bazel-diff JAR not found at {jar_path}", file=sys.stderr)
            sys.exit(1)

        targets_list = run_bazel_diff(jar_path, workspace, base_sha)

        # Determine targets string
        if targets_list is None:
            # bazel-diff failed
            targets = "//..."
            has_bazel_changes = True
        elif not targets_list:
            # No Bazel targets affected
            targets = ""
            has_bazel_changes = False
            print("No Bazel targets affected")
        else:
            print_truncated(f"Found {len(targets_list)} affected targets", targets_list)
            # If infra changed, use //... for safety but still compute workflows from actual targets
            if infra_changed:
                targets = "//..."
                has_bazel_changes = True
            else:
                targets = " ".join(targets_list)
                has_bazel_changes = True

        # Compute which workflows to run
        triggered = compute_triggered_workflows(
            workflows,
            targets if not infra_changed else " ".join(targets_list or []),
            changed_files,
            has_bazel_changes or infra_changed,
        )

        decision = CIDecision(targets=targets, workflows=triggered, infra_changed=infra_changed)

    # Output results
    output("targets", decision.targets)
    output("workflows", json.dumps(decision.workflows))
    output("infra_changed", bool_str(decision.infra_changed))

    print(f"\nDecision: {len(decision.workflows)} workflows to run")
    for w in decision.workflows:
        print(f"  - {w}")


if __name__ == "__main__":
    main()

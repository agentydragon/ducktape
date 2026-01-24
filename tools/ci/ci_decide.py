#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "pygit2>=1.14", "pyyaml>=6.0"]
# ///
"""CI decision engine - computes affected targets and workflows to run.

Reads workflow definitions from workflows.yaml and uses bazel-diff to compute
exactly which Bazel targets are affected. Outputs a JSON list of workflows
to trigger instead of individual boolean flags.

Requires GITHUB_OUTPUT environment variable to be set.
Requires BAZEL_DIFF_JAR environment variable pointing to bazel-diff JAR.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pygit2
import yaml
from pydantic import BaseModel, Field

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


class WorkflowTrigger(BaseModel):
    """Trigger configuration for a workflow."""

    name: str
    bazel_pattern: str | None = None
    path_pattern: str | None = None
    always: bool = False


class CIDecision(BaseModel):
    """Result of CI decision computation."""

    targets: list[str] = Field(default_factory=list)  # List of affected Bazel targets
    all_targets: bool = False  # True means "//..." (rebuild everything)
    workflows: list[str] = Field(default_factory=list)
    infra_changed: bool = False

    @property
    def targets_str(self) -> str:
        """Return targets as space-separated string for GitHub Actions output."""
        if self.all_targets:
            return "//..."
        return " ".join(self.targets)

    def write_to_github_output(self, output_path: Path) -> None:
        """Write decision to GitHub Actions output file."""
        with output_path.open("a") as f:
            f.write(f"targets={self.targets_str}\n")
            f.write(f"workflows={json.dumps(self.workflows)}\n")
            f.write(f"infra_changed={'true' if self.infra_changed else 'false'}\n")


def load_workflows(manifest_path: Path) -> dict[str, WorkflowTrigger]:
    """Load workflow definitions from YAML manifest."""
    with manifest_path.open() as f:
        data = yaml.safe_load(f)

    return {
        name: WorkflowTrigger(
            name=name,
            bazel_pattern=config.get("bazel_pattern"),
            path_pattern=config.get("path_pattern"),
            always=config.get("always", False),
        )
        for name, config in data.items()
    }


def get_base_commit(repo: pygit2.Repository) -> pygit2.Commit | None:
    """Determine base commit for comparison."""
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
            print(f"Pull request: comparing against merge-base {str(merge_base_oid)[:8]}")
            return repo.get(merge_base_oid)
        except (KeyError, pygit2.GitError):
            return None

    # Push event: compare against parent commit
    try:
        head_commit = repo.head.peel(pygit2.Commit)
        if head_commit.parents:
            parent = head_commit.parents[0]
            print(f"Push: comparing against HEAD~1 ({str(parent.id)[:8]})")
            return parent
    except (KeyError, pygit2.GitError):
        pass
    return None


def get_changed_files(repo: pygit2.Repository, base_commit: pygit2.Commit) -> list[str]:
    """Get list of files changed between base commit and HEAD."""
    head_commit = repo.head.peel(pygit2.Commit)
    diff = repo.diff(base_commit, head_commit)
    return [delta.new_file.path for delta in diff.deltas]


def has_infra_changes(changed_files: list[str]) -> bool:
    """Check if any changed files match infrastructure patterns."""
    compiled = [re.compile(p) for p in INFRA_PATTERNS]
    return any(r.match(f) for r in compiled for f in changed_files)


def checkout_commit(repo: pygit2.Repository, commit: pygit2.Commit) -> None:
    """Checkout a specific commit, updating the working directory."""
    repo.checkout_tree(commit, strategy=pygit2.GIT_CHECKOUT_FORCE)
    repo.set_head(commit.id)


def run_bazel_diff(
    repo: pygit2.Repository, jar_path: Path, workspace: Path, base_commit: pygit2.Commit
) -> list[str] | None:
    """Run bazel-diff to compute impacted targets.

    Returns list of targets, empty list if no changes, or None on failure.
    """
    head_commit = repo.head.peel(pygit2.Commit)

    with tempfile.TemporaryDirectory() as tmpdir:
        base_json = Path(tmpdir) / "base.json"
        head_json = Path(tmpdir) / "head.json"
        targets_file = Path(tmpdir) / "targets.txt"

        # Generate hashes for base commit
        print(f"Generating hashes for base commit {str(base_commit.id)[:8]}...")
        checkout_commit(repo, base_commit)

        result = subprocess.run(
            ["java", "-jar", jar_path, "generate-hashes", "-w", workspace, "-b", "bazelisk", base_json],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("Base hash generation failed")
            checkout_commit(repo, head_commit)
            return None

        # Generate hashes for head commit
        print(f"Generating hashes for head commit {str(head_commit.id)[:8]}...")
        checkout_commit(repo, head_commit)

        result = subprocess.run(
            ["java", "-jar", jar_path, "generate-hashes", "-w", workspace, "-b", "bazelisk", head_json],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("Head hash generation failed")
            return None

        # Compute impacted targets
        print("Computing impacted targets...")
        result = subprocess.run(
            ["java", "-jar", jar_path, "get-impacted-targets", "-sh", base_json, "-fh", head_json, "-o", targets_file],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("Target diff failed")
            return None

        if not targets_file.exists() or targets_file.stat().st_size == 0:
            return []

        return [t for t in targets_file.read_text().strip().split("\n") if t]


def check_bazel_intersection(targets: list[str], pattern: str) -> bool:
    """Check if affected targets intersect with a Bazel pattern."""
    if not targets:
        return False

    targets_str = " ".join(targets)
    query = f"set({targets_str}) intersect {pattern}"
    result = subprocess.run(["bazelisk", "query", query], check=False, capture_output=True, text=True)
    return bool(result.stdout.strip())


def get_workflows_from_file_changes(changed_files: list[str], workflows: dict[str, WorkflowTrigger]) -> set[str]:
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
    workflows: dict[str, WorkflowTrigger], targets: list[str], changed_files: list[str]
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

    # 4. Bazel-pattern workflows (only if we have affected targets)
    if targets:
        for name, config in workflows.items():
            if config.bazel_pattern and check_bazel_intersection(targets, config.bazel_pattern):
                triggered.add(name)

    return sorted(triggered)


def print_truncated(label: str, items: list[str], limit: int = 20) -> None:
    """Print a list, truncating if over limit."""
    print(f"{label}:")
    for item in items[:limit]:
        print(f"  {item}")
    if len(items) > limit:
        print(f"  ... and {len(items) - limit} more")


def main() -> int:
    # Require GITHUB_OUTPUT
    output_path_str = os.environ.get("GITHUB_OUTPUT")
    if not output_path_str:
        print("Error: GITHUB_OUTPUT environment variable not set", file=sys.stderr)
        return 1
    output_path = Path(output_path_str)

    # Load workflow manifest
    script_dir = Path(__file__).parent
    manifest_path = script_dir / "workflows.yaml"
    if not manifest_path.exists():
        print(f"Error: {manifest_path} not found", file=sys.stderr)
        return 1

    workflows = load_workflows(manifest_path)
    print(f"Loaded {len(workflows)} workflow definitions")

    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
    repo = pygit2.Repository(workspace)

    # Get base commit for comparison
    base_commit = get_base_commit(repo)
    if not base_commit:
        print("No base commit (new branch or initial commit), triggering all workflows")
        decision = CIDecision(all_targets=True, workflows=sorted(workflows.keys()), infra_changed=True)
    else:
        # Get changed files
        changed_files = get_changed_files(repo, base_commit)
        print_truncated("Changed files", changed_files)

        # Check for infrastructure changes
        infra_changed = has_infra_changes(changed_files)
        if infra_changed:
            print("Infrastructure change detected")

        # Require BAZEL_DIFF_JAR
        jar_path_str = os.environ.get("BAZEL_DIFF_JAR")
        if not jar_path_str:
            print("Error: BAZEL_DIFF_JAR environment variable not set", file=sys.stderr)
            return 1

        jar_path = Path(jar_path_str)
        if not jar_path.exists():
            print(f"Error: bazel-diff JAR not found at {jar_path}", file=sys.stderr)
            return 1

        targets = run_bazel_diff(repo, jar_path, workspace, base_commit)

        # Handle bazel-diff result
        all_targets = False
        if targets is None:
            # bazel-diff failed, build everything
            print("bazel-diff failed, building all targets")
            targets = []
            all_targets = True
        elif not targets:
            print("No Bazel targets affected")
        else:
            print_truncated(f"Found {len(targets)} affected targets", targets)
            if infra_changed:
                # Infrastructure change means we need to rebuild everything
                all_targets = True

        # Compute which workflows to run
        triggered = compute_triggered_workflows(workflows, targets, changed_files)

        decision = CIDecision(
            targets=targets, all_targets=all_targets, workflows=triggered, infra_changed=infra_changed
        )

    # Write to GITHUB_OUTPUT
    decision.write_to_github_output(output_path)

    print(f"\nDecision: {len(decision.workflows)} workflows to run")
    for w in decision.workflows:
        print(f"  - {w}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

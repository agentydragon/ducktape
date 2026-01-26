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
import tempfile
from pathlib import Path

import pygit2
from models import AlwaysTrigger, BazelPatternTrigger, PathPatternTrigger, WorkflowConfig, WorkflowManifest
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


def get_or_generate_hashes(
    repo: pygit2.Repository, jar_path: Path, workspace: Path, commit: pygit2.Commit, cache_dir: Path
) -> Path:
    """Get cached hashes or generate them for a commit.

    Returns path to the hash JSON file.
    """
    sha = str(commit.id)
    cached_path = cache_dir / f"{sha}.json"

    if cached_path.exists():
        print(f"Using cached hashes for {sha[:8]}")
        return cached_path

    print(f"Generating hashes for {sha[:8]}...")
    current_head = repo.head.peel(pygit2.Commit)
    checkout_commit(repo, commit)

    try:
        subprocess.run(
            ["java", "-jar", jar_path, "generate-hashes", "-w", workspace, "-b", "bazelisk", cached_path],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        # Always restore to original HEAD
        checkout_commit(repo, current_head)

    return cached_path


def run_bazel_diff(
    repo: pygit2.Repository, jar_path: Path, workspace: Path, base_commit: pygit2.Commit
) -> list[str] | None:
    """Run bazel-diff to compute impacted targets.

    Returns list of targets, empty list if no changes, or None on failure.
    """
    head_commit = repo.head.peel(pygit2.Commit)

    # Use cache directory for hash files (persisted via GitHub Actions cache)
    cache_dir = Path(os.environ.get("BAZEL_DIFF_CACHE_DIR", workspace / ".bazel-diff-cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        base_json = get_or_generate_hashes(repo, jar_path, workspace, base_commit, cache_dir)
        head_json = get_or_generate_hashes(repo, jar_path, workspace, head_commit, cache_dir)

        # Compute impacted targets
        print("Computing impacted targets...")
        result = subprocess.run(
            ["java", "-jar", jar_path, "get-impacted-targets", "-sh", base_json, "-fh", head_json],
            check=True,
            capture_output=True,
            text=True,
        )

        return [t for t in result.stdout.strip().split("\n") if t]

    except subprocess.CalledProcessError as e:
        print(f"bazel-diff failed: {e.stderr or e.stdout or e}")
        return None


def check_bazel_intersection(targets: list[str], pattern: str) -> bool:
    """Check if affected targets intersect with a Bazel pattern.

    Uses --query_file to avoid "Argument list too long" errors with large target sets.
    """
    if not targets:
        return False

    targets_str = " ".join(targets)
    query = f"set({targets_str}) intersect {pattern}"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".query", delete_on_close=False) as f:
        f.write(query)
        f.flush()
        result = subprocess.run(
            ["bazelisk", "query", f"--query_file={f.name}"], check=False, capture_output=True, text=True
        )
    return bool(result.stdout.strip())


def should_trigger(name: str, config: WorkflowConfig, targets: list[str], changed_files: list[str]) -> bool:
    """Check if a workflow should be triggered."""
    if f".github/workflows/{name}.yml" in changed_files:
        print(f"Workflow file changed -> triggers {name}")
        return True

    match config.trigger:
        case AlwaysTrigger():
            return True
        case PathPatternTrigger(pattern=pattern):
            regex = re.compile(pattern)
            if any(regex.match(f) for f in changed_files):
                print(f"Path pattern '{pattern}' matched -> triggers {name}")
                return True
        case BazelPatternTrigger(pattern=pattern):
            if targets and check_bazel_intersection(targets, pattern):
                return True

    return False


def get_triggered_workflows(
    workflows: dict[str, WorkflowConfig], targets: list[str], changed_files: list[str]
) -> list[str]:
    """Determine which workflows should run based on changes."""
    return sorted(name for name, config in workflows.items() if should_trigger(name, config, targets, changed_files))


def print_truncated(label: str, items: list[str], limit: int = 20) -> None:
    """Print a list, truncating if over limit."""
    print(f"{label}:")
    for item in items[:limit]:
        print(f"  {item}")
    if len(items) > limit:
        print(f"  ... and {len(items) - limit} more")


def get_bazel_diff_jar() -> Path:
    """Get path to bazel-diff JAR from environment."""
    jar_path_str = os.environ.get("BAZEL_DIFF_JAR")
    if not jar_path_str:
        raise RuntimeError("BAZEL_DIFF_JAR environment variable not set")
    jar_path = Path(jar_path_str)
    if not jar_path.exists():
        raise FileNotFoundError(f"bazel-diff JAR not found at {jar_path}")
    return jar_path


def compute_decision(workflows: dict[str, WorkflowConfig], workspace: Path) -> CIDecision:
    """Compute CI decision based on changes."""
    repo = pygit2.Repository(workspace)
    base_commit = get_base_commit(repo)

    if not base_commit:
        print("No base commit (new branch or initial commit), triggering all workflows")
        return CIDecision(all_targets=True, workflows=sorted(workflows.keys()), infra_changed=True)

    changed_files = get_changed_files(repo, base_commit)
    print_truncated("Changed files", changed_files)

    infra_changed = has_infra_changes(changed_files)
    if infra_changed:
        print("Infrastructure change detected")

    jar_path = get_bazel_diff_jar()
    targets = run_bazel_diff(repo, jar_path, workspace, base_commit)

    all_targets = False
    if targets is None:
        print("bazel-diff failed, building all targets")
        targets = []
        all_targets = True
    elif not targets:
        print("No Bazel targets affected")
    else:
        print_truncated(f"Found {len(targets)} affected targets", targets)
        if infra_changed:
            all_targets = True

    triggered = get_triggered_workflows(workflows, targets, changed_files)
    return CIDecision(targets=targets, all_targets=all_targets, workflows=triggered, infra_changed=infra_changed)


def main() -> None:
    output_path_str = os.environ.get("GITHUB_OUTPUT")
    if not output_path_str:
        raise RuntimeError("GITHUB_OUTPUT environment variable not set")
    output_path = Path(output_path_str)

    script_dir = Path(__file__).parent
    manifest_path = script_dir / "workflows.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest = WorkflowManifest.from_yaml(manifest_path)
    print(f"Loaded {len(manifest.workflows)} workflow definitions")

    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
    decision = compute_decision(manifest.workflows, workspace)

    decision.write_to_github_output(output_path)

    print(f"\nDecision: {len(decision.workflows)} workflows to run")
    for w in decision.workflows:
        print(f"  - {w}")


if __name__ == "__main__":
    main()

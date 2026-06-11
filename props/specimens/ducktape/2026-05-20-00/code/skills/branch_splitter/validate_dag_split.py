#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2"]
# ///
"""Validate branch split against all DAG orderings.

This script validates that a set of PR branches:
1. Merge cleanly in every valid topological ordering of the DAG
2. Don't introduce new test failures compared to baseline (optional)
3. Produce a final diff that exactly equals the original branch diff (content invariant)

Usage:
    ./validate_dag_split.py dag.json [--skip-tests] [--max-orderings N] [--verbose]

DAG JSON format:
{
  "base": "origin/devel",
  "original_branch": "origin/claude/my-feature-branch",
  "remote": "origin",
  "test_command": "bazel test //...",
  "build_command": "bazel build --config=check //...",
  "branches": {
    "pr1-style-fixes": [],
    "pr2-refactor-auth": [],
    "pr3-feature-a": ["pr1-style-fixes", "pr2-refactor-auth"]
  }
}

The 'branches' field maps branch names to their dependencies (empty = no deps).
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel


class DagConfig(BaseModel):
    """Configuration for DAG validation.

    This is a skeleton - customize for your repo by:
    1. Setting appropriate test_command and build_command
    2. Optionally adding pre-commit or other validation hooks
    """

    base: str
    original_branch: str
    branches: dict[str, list[str]]
    remote: str = "origin"  # Remote name for fetching branches
    test_command: str = "true"  # TODO: Set to your test command, e.g. "bazel test //..."
    build_command: str = "true"  # TODO: Set to your build command, e.g. "bazel build --config=check //..."


def parse_dag_config(path: Path) -> DagConfig:
    """Parse DAG configuration from JSON file."""
    return DagConfig.model_validate_json(path.read_text())


def all_topological_sorts(dag: dict[str, list[str]]) -> Iterator[list[str]]:
    """Generate all valid topological orderings of a DAG.

    Uses Kahn's algorithm variant that yields all valid orderings.

    Args:
        dag: Map of node -> list of dependencies (predecessors)

    Yields:
        Each valid topological ordering as a list of node names
    """
    # Build in-degree map and reverse edges
    in_degree: dict[str, int] = {node: len(deps) for node, deps in dag.items()}
    dependents: dict[str, list[str]] = {node: [] for node in dag}
    for node, deps in dag.items():
        for dep in deps:
            if dep not in dependents:
                dependents[dep] = []
            dependents[dep].append(node)

    def backtrack(current: list[str], remaining_in_degree: dict[str, int]) -> Iterator[list[str]]:
        # Find all nodes with in-degree 0 that haven't been added yet
        available = [n for n in dag if remaining_in_degree.get(n, -1) == 0 and n not in current]

        if not available:
            # No more nodes available
            if len(current) == len(dag):
                yield list(current)
            return

        # Try each available node
        for node in available:
            current.append(node)
            # Decrease in-degree of dependents
            new_in_degree = remaining_in_degree.copy()
            del new_in_degree[node]  # Mark as processed
            for dependent in dependents.get(node, []):
                if dependent in new_in_degree:
                    new_in_degree[dependent] -= 1

            yield from backtrack(current, new_in_degree)

            current.pop()

    yield from backtrack([], in_degree.copy())


def run_git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, ["git", *args], result.stdout, result.stderr)
    return result


def get_diff(ref1: str, ref2: str, cwd: Path | None = None) -> str:
    """Get the diff between two refs."""
    result = run_git("diff", f"{ref1}...{ref2}", cwd=cwd)
    return result.stdout


def normalize_diff(diff: str) -> str:
    """Normalize a diff for comparison (strip commit-specific metadata)."""
    lines = []
    for line in diff.splitlines():
        # Skip index lines (contain commit hashes)
        if line.startswith("index "):
            continue
        lines.append(line)
    return "\n".join(lines)


def validate_ordering(
    worktree: Path, config: DagConfig, ordering: list[str], run_tests: bool, verbose: bool
) -> tuple[bool, str]:
    """Validate a single ordering by attempting merges.

    Returns:
        (success, error_message) tuple
    """
    # Reset to base
    run_git("checkout", config.base, cwd=worktree)
    run_git("reset", "--hard", config.base, cwd=worktree)

    for branch in ordering:
        if verbose:
            print(f"    Merging {branch}...")

        # Use configured remote (default: origin)
        branch_ref = f"{config.remote}/{branch}" if config.remote else branch
        result = run_git("merge", "--no-ff", branch_ref, cwd=worktree, check=False)

        if result.returncode != 0:
            return False, f"Conflict merging {branch}: {result.stderr}"

        if run_tests:
            # Run build
            build_result = subprocess.run(
                config.build_command, check=False, shell=True, cwd=worktree, capture_output=True, text=True
            )
            if build_result.returncode != 0:
                return False, f"Build failed after {branch}: {build_result.stderr}"

            # Run tests
            test_result = subprocess.run(
                config.test_command, check=False, shell=True, cwd=worktree, capture_output=True, text=True
            )
            if test_result.returncode != 0:
                return False, f"Tests failed after {branch}: {test_result.stderr}"

    return True, ""


def validate_content_invariant(
    worktree: Path, config: DagConfig, ordering: list[str], original_diff: str, verbose: bool
) -> tuple[bool, str]:
    """Validate that the merged result matches the original branch diff.

    Returns:
        (success, error_message) tuple
    """
    # The worktree should already have all branches merged from last ordering test
    # Get diff from base to current state
    result_diff = get_diff(config.base, "HEAD", cwd=worktree)

    original_normalized = normalize_diff(original_diff)
    result_normalized = normalize_diff(result_diff)

    if original_normalized == result_normalized:
        return True, ""

    # Find differences for reporting
    original_files = set()
    result_files = set()

    for line in original_diff.splitlines():
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                original_files.add(parts[2].removeprefix("a/"))

    for line in result_diff.splitlines():
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                result_files.add(parts[2].removeprefix("a/"))

    missing = original_files - result_files
    extra = result_files - original_files

    error_parts = ["Content invariant violated!"]
    error_parts.append(f"Files in original: {len(original_files)}")
    error_parts.append(f"Files in split union: {len(result_files)}")

    if missing:
        error_parts.append(f"\nFiles MISSING from split: {missing}")
    if extra:
        error_parts.append(f"\nExtra files in split (not in original): {extra}")

    # Write detailed diff comparison for debugging
    if verbose:
        Path("/tmp/original.diff").write_text(original_normalized)
        Path("/tmp/result.diff").write_text(result_normalized)
        error_parts.append("\nDetailed diffs written to /tmp/original.diff and /tmp/result.diff")

    return False, "\n".join(error_parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate branch split against all DAG orderings")
    parser.add_argument("dag_file", type=Path, help="Path to DAG JSON configuration")
    parser.add_argument("--skip-tests", action="store_true", help="Skip running tests (just check merges)")
    parser.add_argument("--max-orderings", type=int, default=100, help="Max orderings to test (default: 100)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    # Parse config
    config = parse_dag_config(args.dag_file)

    print("=== Configuration ===")
    print(f"Base branch: {config.base}")
    print(f"Original branch: {config.original_branch}")
    print(f"PR branches: {len(config.branches)}")

    # Capture original diff
    print("\n=== Capturing original branch diff ===")
    original_diff = get_diff(config.base, config.original_branch)
    original_lines = len(original_diff.splitlines())
    print(f"Original diff: {original_lines} lines")

    if original_lines == 0:
        print("ERROR: Original branch has no diff from base!")
        return 1

    # Generate orderings
    print("\n=== Generating valid DAG orderings ===")
    orderings = list(all_topological_sorts(config.branches))
    print(f"Valid orderings: {len(orderings)}")

    if len(orderings) > args.max_orderings:
        print(f"Sampling {args.max_orderings} of {len(orderings)} orderings")
        # Sample: first, last, and random middle ones
        sampled = [orderings[0], orderings[-1]]
        if len(orderings) > 2:
            middle = random.sample(orderings[1:-1], min(args.max_orderings - 2, len(orderings) - 2))
            sampled.extend(middle)
        orderings = sampled

    # Create temp worktree for validation
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir) / "worktree"

        print(f"\n=== Creating worktree in {worktree} ===")
        run_git("worktree", "add", str(worktree), config.base)

        try:
            # Test each ordering
            print(f"\n=== Testing {len(orderings)} valid DAG orderings ===")
            failures = []

            for i, ordering in enumerate(orderings, 1):
                ordering_str = " -> ".join(ordering)
                if args.verbose:
                    print(f"\n--- Ordering {i}/{len(orderings)}: {ordering_str} ---")
                else:
                    print(f"  Testing ordering {i}/{len(orderings)}...", end=" ")

                success, error = validate_ordering(
                    worktree, config, ordering, run_tests=not args.skip_tests, verbose=args.verbose
                )

                if success:
                    if not args.verbose:
                        print("OK")
                else:
                    if not args.verbose:
                        print("FAILED")
                    failures.append((ordering, error))
                    print(f"  FAIL: {error}")

            if failures:
                print(f"\n=== VALIDATION FAILED: {len(failures)}/{len(orderings)} orderings failed ===")
                return 1

            print(f"\n=== All {len(orderings)} orderings merge cleanly ===")

            # Content invariant check - use any ordering (last one is still in worktree)
            print("\n=== Verifying content invariant (split union = original diff) ===")

            # Reset and replay the first ordering to get clean state
            run_git("checkout", config.base, cwd=worktree)
            run_git("reset", "--hard", config.base, cwd=worktree)
            for branch in orderings[0]:
                branch_ref = f"{config.remote}/{branch}" if config.remote else branch
                run_git("merge", "--no-ff", branch_ref, cwd=worktree)

            success, error = validate_content_invariant(worktree, config, orderings[0], original_diff, args.verbose)

            if success:
                print("Content invariant PASSED: split union equals original diff")
            else:
                print(f"FAIL: {error}")
                return 1

        finally:
            # Clean up worktree
            run_git("worktree", "remove", "--force", str(worktree))

    print("\n=== VALIDATION PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

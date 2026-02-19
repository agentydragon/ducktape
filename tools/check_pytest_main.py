#!/usr/bin/env python3
"""Check that py_test files have pytest_bazel.main() entry points.

This guard prevents the silent test failure mode where py_test targets
without pytest_bazel.main() import the test file as a module and exit 0
without running any tests.

Usage:
    # Via Bazel (recommended, uses caching)
    bazel run //tools:check_pytest_main -- --all

    # Via pre-commit (checks changed files)
    pre-commit run check-pytest-main

    # Direct invocation
    tools/check_pytest_main.py test_foo.py test_bar.py
    tools/check_pytest_main.py --all

Detection method:
    Queries Bazel for all py_test targets and their srcs/main attributes via
    bazel_util.query. Two concurrent queries are run:
      - labels(srcs, kind(py_test, //...))  — all py_test source files
      - labels(srcs, attr(main, ".+", kind(py_test, //...)))  — sources in
        targets with a custom main= entry point

    The Bazel query approach correctly handles Starlark macros that expand to
    py_test (e.g. live_openai_py_test), and correctly skips files that are not
    part of any py_test target (e.g. specimen/snapshot code in props/).

    Files marked as lint-ignored in .gitattributes (rules-lint-ignored=true,
    linguist-generated=true, or gitlab-generated=true) are excluded from
    checking. This is the authoritative way to exclude files like specimen code.

TODO: Add XML analysis safety net that checks JUnit XML test results
      after Bazel test execution to detect tests that collected 0 tests.
      This would catch cases where pytest_bazel.main() was added but
      never actually executed.

Exit codes:
    0: All checks passed
    1: Found tests missing pytest_bazel.main()
    2: Invalid usage or system error
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import pygit2

from bazel_util.query import run_query
from bazel_util.workspace import get_build_workspace_directory
from tools.precommit.lint_ignored import is_lint_ignored, try_open_repo

# Pre-compiled regex patterns
_TEST_FUNC_PATTERN = re.compile(r"^\s*(async\s+)?def\s+test_\w+", re.MULTILINE)
_HELPER_PATTERNS = [re.compile(r"test_helpers?\.py$"), re.compile(r"test_utils?\.py$"), re.compile(r"testing/.*\.py$")]

# Number of worker threads for parallel file checking
_MAX_WORKERS = min(32, (os.cpu_count() or 4) + 4)


class CheckResult(NamedTuple):
    """Result of checking a single test file."""

    file_path: Path
    passed: bool
    reason: str


@dataclass
class BazelPyTestIndex:
    """Index of py_test srcs built from bazel query output.

    Covers all py_test targets reachable from //... in the workspace.
    """

    # Resolved absolute paths of all source files in any py_test's srcs
    known_srcs: set[Path] = field(default_factory=set)
    # Subset of known_srcs that are in a py_test with a custom main= attribute.
    # Files in this set don't need pytest_bazel.main() because the custom main
    # handles test dispatch.
    exempt_srcs: set[Path] = field(default_factory=set)


def try_build_bazel_index(repo_root: Path) -> BazelPyTestIndex | None:
    """Query Bazel for all py_test targets and build a src-file index.

    Runs two concurrent bazel queries via bazel_util.query.run_query:
      - All source files that are srcs of some py_test target
      - Source files that are srcs of a py_test with a custom main= attribute

    Returns None if bazel is unavailable, the server is not running, or the
    query otherwise fails (e.g. inside a Bazel sandbox test action).
    """
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            all_fut = executor.submit(run_query, "labels(srcs, kind(py_test, //...))", cwd=repo_root)
            exempt_fut = executor.submit(
                run_query, "labels(srcs, attr(main, '.+', kind(py_test, //...)))", cwd=repo_root
            )
            all_srcs_labels = all_fut.result()
            exempt_srcs_labels = exempt_fut.result()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    index = BazelPyTestIndex()
    for label in all_srcs_labels:
        if label.path is not None:
            index.known_srcs.add((repo_root / label.path).resolve())
    for label in exempt_srcs_labels:
        if label.path is not None:
            index.exempt_srcs.add((repo_root / label.path).resolve())

    return index


def has_test_functions(content: str) -> bool:
    """Check if Python content has test functions."""
    return bool(_TEST_FUNC_PATTERN.search(content))


def has_pytest_bazel_main(content: str) -> bool:
    """Check if content has pytest_bazel.main() call."""
    return "pytest_bazel.main()" in content


def should_skip_file(file_path: Path) -> tuple[bool, str]:
    """Check if file should be skipped from checking.

    Returns (should_skip, reason). Does not check gitattributes — that
    filtering happens at the file-list level in main() and find_all_test_files().
    """
    if file_path.name == "conftest.py":
        return True, "conftest.py (fixture file)"

    file_path_str = str(file_path)

    if "external/" in file_path_str:
        return True, "external dependency"

    if any(part.startswith("bazel-") for part in file_path.parts):
        return True, "bazel output directory"

    for pattern in _HELPER_PATTERNS:
        if pattern.search(file_path_str):
            return True, f"test helper (matches {pattern.pattern})"

    return False, ""


def check_file(file_path: Path, repo_root: Path, bazel_index: BazelPyTestIndex | None) -> CheckResult:
    """Check if test file has required pytest_bazel.main() entry point."""
    should_skip, skip_reason = should_skip_file(file_path)
    if should_skip:
        return CheckResult(file_path, True, f"skipped: {skip_reason}")

    try:
        content = (repo_root / file_path).read_text()
    except OSError as e:
        return CheckResult(file_path, False, f"error reading file: {e}")

    if not has_test_functions(content):
        return CheckResult(file_path, True, "no test functions")

    if has_pytest_bazel_main(content):
        return CheckResult(file_path, True, "has pytest_bazel.main()")

    # Determine whether the file is exempt from needing pytest_bazel.main().
    abs_path = (repo_root / file_path).resolve() if not file_path.is_absolute() else file_path.resolve()

    if bazel_index is not None:
        # Bazel query path: authoritative source of truth.
        if abs_path not in bazel_index.known_srcs:
            # File is not part of any py_test target — skip it.
            return CheckResult(file_path, True, "not a py_test src (not a Bazel target)")
        if abs_path in bazel_index.exempt_srcs:
            return CheckResult(file_path, True, "exempt: py_test uses custom main= (bazel query)")

    # Check if using pytest.main() directly (custom runner)
    if "pytest.main(" in content:
        return CheckResult(file_path, True, "uses pytest.main() (custom runner)")

    return CheckResult(file_path, False, "has test functions but missing pytest_bazel.main() entry point")


def find_all_test_files(
    repo_root: Path, bazel_index: BazelPyTestIndex | None, git_repo: pygit2.Repository | None
) -> list[Path]:
    """Return the list of test files to check.

    When a bazel_index is available, returns only the files that are actual
    py_test srcs (skipping specimen/snapshot code and other non-Bazel files).
    Falls back to rglob when the index is unavailable.

    In both cases, files marked as lint-ignored via .gitattributes are excluded.
    """
    if bazel_index is not None:
        # Only check files that are actually registered py_test srcs.
        candidates = [p for p in bazel_index.known_srcs if p.name.startswith("test_") and p.name.endswith(".py")]
    else:
        candidates = []
        for py_file in repo_root.rglob("test_*.py"):
            if any(part.startswith("bazel-") for part in py_file.parts):
                continue
            if "external/" in str(py_file):
                continue
            candidates.append(py_file)

    if git_repo is None:
        return candidates

    # Filter out files marked as lint-ignored in .gitattributes.
    return [p for p in candidates if not is_lint_ignored(git_repo, p.relative_to(repo_root))]


async def check_files_async(
    files: list[Path], repo_root: Path, bazel_index: BazelPyTestIndex | None
) -> list[CheckResult]:
    """Check files in parallel using asyncio."""
    return list(await asyncio.gather(*[asyncio.to_thread(check_file, f, repo_root, bazel_index) for f in files]))


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Check that py_test files have pytest_bazel.main() entry points")
    parser.add_argument(
        "files", nargs="*", type=Path, help="Test files to check (default: check files from stdin or --all)"
    )
    parser.add_argument("--all", action="store_true", help="Check all test_*.py files in repository")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show all results including passes")

    args = parser.parse_args()

    workspace_root = get_build_workspace_directory()

    # Try to open the git repo for gitattributes filtering. Returns None if not
    # in a git repo (e.g. inside a Bazel sandbox test action).
    git_repo = try_open_repo(workspace_root)

    # Run bazel query only for --all mode. Per-file/pre-commit mode skips the
    # query for speed; gitattributes filtering already excludes non-Bazel files
    # (specimens, generated code, etc.) before files reach this tool.
    bazel_index: BazelPyTestIndex | None = None
    if args.all:
        bazel_index = try_build_bazel_index(workspace_root)
        if bazel_index is None and args.verbose:
            print("Note: bazel query unavailable, using rglob file discovery", file=sys.stderr)

    if args.all:
        files = find_all_test_files(workspace_root, bazel_index, git_repo)
        print(f"Checking {len(files)} test files in repository...", file=sys.stderr)
    elif args.files:
        files = [(workspace_root / f) if not f.is_absolute() else f for f in args.files]
        if git_repo is not None:
            files = [f for f in files if not is_lint_ignored(git_repo, f.relative_to(workspace_root))]
    else:
        lines = sys.stdin.read().strip().split("\n")
        files_raw = [(workspace_root / line.strip()) for line in lines if line.strip()]
        if git_repo is not None:
            files = [f for f in files_raw if not is_lint_ignored(git_repo, f.relative_to(workspace_root))]
        else:
            files = files_raw

    if not files:
        print("No files to check", file=sys.stderr)
        return 0

    results = asyncio.run(check_files_async(files, workspace_root, bazel_index))

    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    if args.verbose:
        for result in passed:
            print(f"✓ {result.file_path}: {result.reason}")

    for result in failed:
        print(f"❌ {result.file_path}: {result.reason}", file=sys.stderr)

    if failed:
        print(f"\n{len(failed)} file(s) missing pytest_bazel.main()", file=sys.stderr)
        print("\nTo fix, add this to the end of each failing test file:", file=sys.stderr)
        print("  import pytest_bazel", file=sys.stderr)
        print('  if __name__ == "__main__":', file=sys.stderr)
        print("      pytest_bazel.main()", file=sys.stderr)
        return 1

    if args.verbose or args.all:
        print(f"\n✓ All {len(results)} files passed", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    Parses BUILD.bazel files with regex to find custom main= parameters.
    Works in all environments: workspace, Bazel sandbox, CI, GitHub Actions.
    No Bazel server or query dependencies needed.

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
import sys
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from bazel_util import get_workspace_root

# Pre-compiled regex patterns for performance
_TEST_FUNC_PATTERN = re.compile(r"^\s*(async\s+)?def\s+test_\w+", re.MULTILINE)
_PY_TEST_BLOCK_PATTERN = re.compile(r"py_test\s*\([^)]*?srcs\s*=\s*\[[^\]]*?\][^)]*?\)", re.DOTALL)
_MAIN_PARAM_PATTERN = re.compile(r'main\s*=\s*"([^"]+)"')
_HELPER_PATTERNS = [re.compile(r"test_helpers?\.py$"), re.compile(r"test_utils?\.py$"), re.compile(r"testing/.*\.py$")]

# Number of worker threads for parallel file checking
_MAX_WORKERS = min(32, (os.cpu_count() or 4) + 4)


class CheckResult(NamedTuple):
    """Result of checking a single test file."""

    file_path: Path
    passed: bool
    reason: str


def has_test_functions(content: str) -> bool:
    """Check if Python content has test functions."""
    return bool(_TEST_FUNC_PATTERN.search(content))


def has_pytest_bazel_main(content: str) -> bool:
    """Check if content has pytest_bazel.main() call."""
    return "pytest_bazel.main()" in content


@lru_cache(maxsize=256)
def _read_build_file(build_file: Path) -> str | None:
    """Read and cache BUILD file contents."""
    try:
        return build_file.read_text()
    except OSError:
        return None


def parse_build_file_for_target(build_file: Path, test_file: Path) -> dict | None:
    """Find py_test target that includes test_file in its srcs.

    Returns dict with 'main' key if custom main= parameter found.
    """
    content = _read_build_file(build_file)
    if content is None:
        return None

    test_filename = test_file.name

    # Calculate relative path from BUILD file directory
    build_dir = build_file.parent
    try:
        rel_path = test_file.relative_to(build_dir)
        rel_path_str = str(rel_path)
    except ValueError:
        rel_path_str = test_filename

    # Find all py_test blocks
    for match in _PY_TEST_BLOCK_PATTERN.finditer(content):
        block = match.group(0)

        # Check if this target includes our test file (by filename or relative path)
        if f'"{test_filename}"' in block or f'"{rel_path_str}"' in block:
            # Check if it has main= parameter
            main_match = _MAIN_PARAM_PATTERN.search(block)
            if main_match:
                return {"main": main_match.group(1)}

    return None


def find_build_file(test_file: Path, repo_root: Path) -> Path | None:
    """Find BUILD.bazel file for test_file by walking up directories."""
    current = test_file.parent

    while current >= repo_root:
        build_file = current / "BUILD.bazel"
        if build_file.exists():
            return build_file
        if current == repo_root:
            break
        current = current.parent

    return None


def should_skip_file(file_path: Path) -> tuple[bool, str]:
    """Check if file should be skipped from checking.

    Returns (should_skip, reason).
    """
    # Skip conftest.py files
    if file_path.name == "conftest.py":
        return True, "conftest.py (fixture file)"

    file_path_str = str(file_path)

    # Skip files in external/ directory
    if "external/" in file_path_str:
        return True, "external dependency"

    # Skip bazel output directories
    if any(part.startswith("bazel-") for part in file_path.parts):
        return True, "bazel output directory"

    # Skip common test helper patterns
    for pattern in _HELPER_PATTERNS:
        if pattern.search(file_path_str):
            return True, f"test helper (matches {pattern.pattern})"

    return False, ""


def check_file(file_path: Path, repo_root: Path) -> CheckResult:
    """Check if test file has required pytest_bazel.main() entry point."""
    # Skip certain files
    should_skip, skip_reason = should_skip_file(file_path)
    if should_skip:
        return CheckResult(file_path, True, f"skipped: {skip_reason}")

    # Read file content
    try:
        content = file_path.read_text()
    except OSError as e:
        return CheckResult(file_path, False, f"error reading file: {e}")

    # Check if file has test functions
    if not has_test_functions(content):
        return CheckResult(file_path, True, "no test functions")

    # Check if has pytest_bazel.main()
    if has_pytest_bazel_main(content):
        return CheckResult(file_path, True, "has pytest_bazel.main()")

    # Check if BUILD file specifies custom main=
    build_file = find_build_file(file_path, repo_root)
    if build_file:
        target_info = parse_build_file_for_target(build_file, file_path)
        if target_info and "main" in target_info:
            return CheckResult(file_path, True, f"uses custom main={target_info['main']}")

    # Check if using pytest.main() directly (custom runner)
    if "pytest.main(" in content:
        return CheckResult(file_path, True, "uses pytest.main() (custom runner)")

    # Missing entry point!
    return CheckResult(file_path, False, "has test functions but missing pytest_bazel.main() entry point")


def find_all_test_files(repo_root: Path) -> list[Path]:
    """Find all test_*.py files in repository."""
    test_files = []

    for py_file in repo_root.rglob("test_*.py"):
        # Skip bazel output directories
        if any(part.startswith("bazel-") for part in py_file.parts):
            continue

        # Skip external dependencies
        if "external/" in str(py_file):
            continue

        test_files.append(py_file)

    return test_files


async def check_files_async(files: list[Path], repo_root: Path) -> list[CheckResult]:
    """Check files in parallel using asyncio."""
    return list(await asyncio.gather(*[asyncio.to_thread(check_file, f, repo_root) for f in files]))


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Check that py_test files have pytest_bazel.main() entry points")
    parser.add_argument(
        "files", nargs="*", type=Path, help="Test files to check (default: check files from stdin or --all)"
    )
    parser.add_argument("--all", action="store_true", help="Check all test_*.py files in repository")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show all results including passes")

    args = parser.parse_args()

    # Determine files to check
    workspace_root = get_workspace_root()

    if args.all:
        files = find_all_test_files(workspace_root)
        print(f"Checking {len(files)} test files in repository...", file=sys.stderr)
    elif args.files:
        # Resolve relative paths against workspace root
        files = [(workspace_root / f) if not f.is_absolute() else f for f in args.files]
    else:
        # Read from stdin (for pre-commit)
        lines = sys.stdin.read().strip().split("\n")
        files = [(workspace_root / line.strip()) for line in lines if line.strip()]

    if not files:
        print("No files to check", file=sys.stderr)
        return 0

    # Check files in parallel
    results = asyncio.run(check_files_async(files, workspace_root))

    # Categorize results
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    # Show results
    if args.verbose:
        for result in passed:
            print(f"✓ {result.file_path}: {result.reason}")

    for result in failed:
        print(f"❌ {result.file_path}: {result.reason}", file=sys.stderr)

    # Summary
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

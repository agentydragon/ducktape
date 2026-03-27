"""Check that py_test files have pytest_bazel.main() entry points.

This guard prevents the silent test failure mode where py_test targets
without pytest_bazel.main() import the test file as a module and exit 0
without running any tests.

Detection method:
    Queries Bazel for all py_test targets and their srcs/main attributes via
    bazel_util.query. Two concurrent queries are run:
      - labels(srcs, kind(py_test, //...))  — all py_test source files
      - labels(srcs, attr(main, ".+", kind(py_test, //...)))  — sources in
        targets with a custom main= entry point

    The Bazel query approach correctly handles Starlark macros that expand to
    py_test (e.g. live_openai_py_test), and correctly skips files that are not
    part of any py_test target (e.g. specimen/snapshot code in props/).

TODO: Add XML analysis safety net that checks JUnit XML test results
      after Bazel test execution to detect tests that collected 0 tests.
      This would catch cases where pytest_bazel.main() was added but
      never actually executed.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from util.bazel.workspace import BazelWorkspace


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


def build_bazel_index(repo_root: Path) -> BazelPyTestIndex:
    """Query Bazel for all py_test targets and build a src-file index.

    Runs two concurrent bazel queries via BazelWorkspace.query:
      - All source files that are srcs of some py_test target
      - Source files that are srcs of a py_test with a custom main= attribute
    """
    workspace = BazelWorkspace(root=repo_root)
    with ThreadPoolExecutor(max_workers=2) as executor:
        all_fut = executor.submit(workspace.query, "labels(srcs, kind(py_test, //...))")
        exempt_fut = executor.submit(workspace.query, "labels(srcs, attr(main, '.+', kind(py_test, //...)))")
        all_srcs_labels = all_fut.result()
        exempt_srcs_labels = exempt_fut.result()

    index = BazelPyTestIndex()
    for label in all_srcs_labels:
        if label.path is not None:
            index.known_srcs.add((repo_root / label.path).resolve())
    for label in exempt_srcs_labels:
        if label.path is not None:
            index.exempt_srcs.add((repo_root / label.path).resolve())

    return index


def has_pytest_bazel_main(content: str) -> bool:
    """Check if content has pytest_bazel.main() call."""
    return "pytest_bazel.main()" in content


def check_file(file_path: Path, repo_root: Path, bazel_index: BazelPyTestIndex) -> CheckResult:
    """Check if test file has required pytest_bazel.main() entry point."""
    content = (repo_root / file_path).read_text()

    if has_pytest_bazel_main(content):
        return CheckResult(file_path, passed=True, reason="has pytest_bazel.main()")

    abs_path = (repo_root / file_path).resolve()
    if abs_path not in bazel_index.known_srcs:
        return CheckResult(file_path, passed=True, reason="not a py_test src (not a Bazel target)")
    if abs_path in bazel_index.exempt_srcs:
        return CheckResult(file_path, passed=True, reason="exempt: py_test uses custom main= (bazel query)")

    # Check if using pytest.main() directly (custom runner)
    if "pytest.main(" in content:
        return CheckResult(file_path, passed=True, reason="uses pytest.main() (custom runner)")

    return CheckResult(file_path, passed=False, reason="missing pytest_bazel.main() entry point")


async def check_files_async(files: list[Path], repo_root: Path, bazel_index: BazelPyTestIndex) -> list[CheckResult]:
    """Check files in parallel using asyncio."""
    return list(await asyncio.gather(*[asyncio.to_thread(check_file, f, repo_root, bazel_index) for f in files]))

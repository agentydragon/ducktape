"""Benchmark: measure wall-clock time for Bazel test target discovery strategies.

Benchmarks from a cold Bazel server (shut down before each section):

1. Affected-target discovery via find_affected_tests (validate + rdeps)
2. Simple kind queries: all py_test, all go_test, all *_test targets
3. Alternative strategies: somepath, allrdeps

Usage:
    bazel run //x/enforce_bazel_tests:bench
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

import pygit2

from util.bazel.workspace import BazelWorkspace, get_build_workspace_directory
from util.fs import restore_file
from x.enforce_bazel_tests.enforce_bazel_tests import find_affected_tests

# The file we'll temporarily modify to simulate a change.
_TARGET_FILE = Path("util/bazel/workspace.py")
_SENTINEL = "\n# benchmark-sentinel-comment\n"
_QUERY_TIMEOUT = 300


def _assert_index_clean(repo: pygit2.Repository) -> None:
    """Raise if the index (staging area) has staged changes."""
    staged = {
        path: flags
        for path, flags in repo.status().items()
        if flags & (pygit2.GIT_STATUS_INDEX_NEW | pygit2.GIT_STATUS_INDEX_MODIFIED | pygit2.GIT_STATUS_INDEX_DELETED)
    }
    if staged:
        raise RuntimeError(f"git index is not clean ({len(staged)} staged files). Commit or stash first.")


_T = TypeVar("_T", bound=Sequence)


def _timed_run(label: str, fn: Callable[[], _T]) -> _T | None:
    """Run *fn*, print elapsed time with *label*, and handle subprocess errors."""
    t0 = time.monotonic()
    try:
        result = fn()
    except subprocess.CalledProcessError as e:
        elapsed = time.monotonic() - t0
        print(f"  {label + ':':.<40s} {elapsed:6.2f}s  FAILED (exit {e.returncode})")
        if e.stdout:
            print(f"    stdout: {e.stdout.strip()[:200]}")
        if e.stderr:
            print(f"    stderr: {e.stderr.strip()[:200]}")
        return None
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        print(f"  {label + ':':.<40s} {elapsed:6.2f}s  TIMEOUT")
        return None
    elapsed = time.monotonic() - t0
    print(f"  {label + ':':.<40s} {elapsed:6.2f}s  ({len(result)} targets)")
    return result


def _bench_query(workspace: BazelWorkspace, expr: str) -> None:
    """Run a query from cold start, print timing and result count."""
    workspace.shutdown()
    _timed_run(expr, lambda: workspace.query(expr, timeout=_QUERY_TIMEOUT))


def main() -> int:
    repo_root = get_build_workspace_directory()
    repo = pygit2.Repository(str(repo_root))
    _assert_index_clean(repo)

    workspace = BazelWorkspace(root=repo_root)
    label = workspace.file_to_label(_TARGET_FILE)
    if label is None:
        raise ValueError(f"No BUILD file found for {_TARGET_FILE}")

    print(f"target file:  {_TARGET_FILE}")
    print(f"bazel label:  {label}")
    print()

    # --- Section 1: affected-target discovery (cold) ---
    print("=== affected-target discovery (cold) ===")
    target_path = repo_root / _TARGET_FILE

    with restore_file(target_path):
        target_path.write_text(target_path.read_text() + _SENTINEL)
        workspace.shutdown()

        targets = _timed_run(
            "find_affected_tests",
            lambda: find_affected_tests(workspace, [label], timeout=_QUERY_TIMEOUT),
        ) or []

        for t in sorted(str(lbl) for lbl in targets):
            print(f"    {t}")

    # --- Section 2: simple kind queries (each from cold) ---
    print("\n=== kind queries (each from cold start) ===")
    _bench_query(workspace, 'kind("py_test", //...)')
    _bench_query(workspace, 'kind("go_test", //...)')
    _bench_query(workspace, 'kind(".*_test", //...)')

    # --- Section 3: alternative strategies (each from cold start) ---
    print("\n=== alternative strategies (each from cold start) ===")
    _bench_query(workspace, "//...")
    _bench_query(workspace, f"rdeps(//..., {label})")

    # somepath: find all tests, then filter to those with a path to the changed label.
    # This is the "find tests first, then check deps" approach.
    _bench_query(workspace, f'somepath(kind(".*_test", //...), {label})')

    # allrdeps with kind filter: equivalent to rdeps but may use different evaluation order.
    _bench_query(workspace, f'kind(".*_test", allrdeps({label}))')

    return 0


if __name__ == "__main__":
    sys.exit(main())

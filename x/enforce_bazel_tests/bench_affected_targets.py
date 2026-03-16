#!/usr/bin/env python3
"""Benchmark: measure wall-clock time for Bazel test target discovery strategies.

Benchmarks from a cold Bazel server (shut down before each section):

1. Affected-target discovery via find_affected_tests (validate + rdeps)
2. Simple kind queries: all py_test, all go_test, all *_test targets

Usage:
    bazel run //x/enforce_bazel_tests:bench_affected_targets
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pygit2

from util.bazel.workspace import BazelWorkspace
from util.fs import restore_file
from x.enforce_bazel_tests.enforce_bazel_tests import find_affected_tests

# The file we'll temporarily modify to simulate a change.
_TARGET_FILE = Path("util/bazel/workspace.py")
_SENTINEL = "\n# benchmark-sentinel-comment\n"


def _assert_git_clean(repo: pygit2.Repository) -> None:
    """Raise if the working tree has uncommitted changes."""
    dirty = {path: flags for path, flags in repo.status().items() if flags != pygit2.GIT_STATUS_IGNORED}
    if dirty:
        raise RuntimeError(f"git working tree is not clean ({len(dirty)} dirty files). Commit or stash first.")


def _bench_query(workspace: BazelWorkspace, name: str, expr: str) -> None:
    """Run a query from cold start, print timing and result count."""
    workspace.shutdown()
    t0 = time.monotonic()
    results = workspace.query(expr)
    elapsed = time.monotonic() - t0
    print(f"  {name + ':':.<40s} {elapsed:6.2f}s  ({len(results)} targets)")


def main() -> int:
    repo = pygit2.Repository(".")
    repo_root = Path(repo.workdir).resolve()
    _assert_git_clean(repo)

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

        t0 = time.monotonic()
        targets = find_affected_tests(workspace, [label])
        elapsed = time.monotonic() - t0

        print(f"  find_affected_tests:{'':.<19s} {elapsed:6.2f}s  ({len(targets)} targets)")
        for t in sorted(str(lbl) for lbl in targets):
            print(f"    {t}")

    # --- Section 2: simple kind queries (each from cold) ---
    print("\n=== kind queries (each from cold start) ===")
    _bench_query(workspace, 'kind("py_test", //...)', 'kind("py_test", //...)')
    _bench_query(workspace, 'kind("go_test", //...)', 'kind("go_test", //...)')
    _bench_query(workspace, 'kind(".*_test", //...)', 'kind(".*_test", //...)')

    return 0


if __name__ == "__main__":
    sys.exit(main())

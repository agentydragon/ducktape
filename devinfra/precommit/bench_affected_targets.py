#!/usr/bin/env python3
"""Benchmark: measure wall-clock time to discover affected Bazel test targets.

Measures the two-step query approach used by enforce_bazel_tests.py:
1. Validate source file labels via kind("source file", ...)
2. Find affected test targets via kind(".*_test", rdeps(...))

Runs with a cold Bazel server (shuts it down before each run) and a
single small change to util/bazel/workspace.py (a comment appended).

Usage:
    bazel run //devinfra/precommit:bench_affected_targets
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pygit2

from devinfra.precommit.enforce_bazel_tests import find_affected_tests
from util.bazel.workspace import BazelWorkspace
from util.fs import restore_file

# The file we'll temporarily modify to simulate a change.
_TARGET_FILE = Path("util/bazel/workspace.py")
_SENTINEL = "\n# benchmark-sentinel-comment\n"


def _assert_git_clean(repo: pygit2.Repository) -> None:
    """Raise if the working tree has uncommitted changes."""
    dirty = {path: flags for path, flags in repo.status().items() if flags != pygit2.GIT_STATUS_IGNORED}
    if dirty:
        raise RuntimeError(f"git working tree is not clean ({len(dirty)} dirty files). Commit or stash first.")


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
    print(f"package:      {label.package}")
    print()

    target_path = repo_root / _TARGET_FILE

    with restore_file(target_path):
        # Append a temporary comment to simulate a change.
        target_path.write_text(target_path.read_text() + _SENTINEL)

        # Shut down the Bazel server for a cold-start measurement.
        print("shutting down bazel server...")
        workspace.shutdown()

        print("starting benchmark (cold bazel server)...\n")
        t0 = time.monotonic()

        targets = find_affected_tests(workspace, [label])

        t_total = time.monotonic() - t0

        print(f"total:  {t_total:6.2f}s")
        print(f"\naffected tests ({len(targets)}):")
        for t in sorted(str(lbl) for lbl in targets):
            print(f"  {t}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

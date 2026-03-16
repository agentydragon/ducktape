#!/usr/bin/env python3
"""Benchmark: measure wall-clock time to discover affected Bazel test targets.

Measures the two-step query approach used by enforce_bazel_tests.py:
1. Validate source file labels via kind("source file", ...)
2. Find affected test targets via kind(".*_test", rdeps(...))

Runs with a cold Bazel server (shuts it down before each run) and a
single small change to util/bazel/query.py (a comment appended).

Usage:
    bazel run //devinfra/precommit:bench_affected_targets
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from util.bazel.query import BazelLabel, run_query

# The file we'll temporarily modify to simulate a staged change.
_TARGET_FILE = "util/bazel/query.py"
_SENTINEL = "\n# benchmark-sentinel-comment\n"


def _repo_root() -> Path:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
    return Path(result.stdout.strip())


def _git_is_clean(repo_root: Path) -> None:
    """Raise if the working tree has uncommitted changes."""
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True, cwd=repo_root)
    if result.stdout.strip():
        print("ERROR: git working tree is not clean. Commit or stash first.", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
        sys.exit(1)


def _shutdown_bazel(repo_root: Path) -> None:
    subprocess.run(["bazel", "shutdown"], cwd=repo_root, check=True, capture_output=True)


def _find_bazel_package(filepath: Path, repo_root: Path) -> Path | None:
    current = repo_root / filepath.parent
    while current >= repo_root:
        if (current / "BUILD.bazel").exists() or (current / "BUILD").exists():
            return current.relative_to(repo_root)
        if current == repo_root:
            break
        current = current.parent
    return None


def _file_to_label(filepath: str, repo_root: Path) -> str:
    path = Path(filepath)
    pkg = _find_bazel_package(path, repo_root)
    if pkg is None:
        raise ValueError(f"No BUILD file found for {filepath}")
    pkg_str = "" if pkg == Path() else str(pkg)
    rel = path.relative_to(pkg) if pkg != Path() else path
    return f"//{pkg_str}:{rel}"


# Excluded packages (same as enforce_bazel_tests.py).
_EXCLUDED_PACKAGES = {"x", "gterm_theme", "bazel-ducktape"}


def _build_universe(repo_root: Path) -> tuple[str, str]:
    """Build universe_expr and universe_scope strings."""
    parts: list[str] = []
    for entry in sorted(repo_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in _EXCLUDED_PACKAGES:
            continue
        if (entry / "BUILD.bazel").exists() or (entry / "BUILD").exists():
            parts.append(f"//{entry.name}/...")
    if (repo_root / "BUILD.bazel").exists() or (repo_root / "BUILD").exists():
        parts.insert(0, "//:all")
    return " + ".join(parts), ",".join(parts)


def main() -> int:
    repo_root = _repo_root()
    _git_is_clean(repo_root)

    label = _file_to_label(_TARGET_FILE, repo_root)
    parsed = BazelLabel.parse(label)
    pkg_str = str(parsed.package)

    print(f"target file:  {_TARGET_FILE}")
    print(f"bazel label:  {label}")
    print(f"package:      {pkg_str}")
    print()

    # Append a temporary comment to simulate a change.
    target_path = repo_root / _TARGET_FILE
    original = target_path.read_text()
    target_path.write_text(original + _SENTINEL)

    try:
        # Shut down the Bazel server for a cold-start measurement.
        print("shutting down bazel server...")
        _shutdown_bazel(repo_root)

        print("starting benchmark (cold bazel server)...\n")
        t0 = time.monotonic()

        # Step 1: validate label
        t1_start = time.monotonic()
        validate_expr = f'kind("source file", //{pkg_str}:*)'
        known_sources = {str(lbl) for lbl in run_query(validate_expr, cwd=repo_root)}
        t1_end = time.monotonic()

        if label not in known_sources:
            print(f"WARNING: {label} is not a known source file")
            return 1

        # Step 2: find affected tests via rdeps
        t2_start = time.monotonic()
        universe_expr, universe_scope = _build_universe(repo_root)
        rdeps_expr = f'kind(".*_test", rdeps({universe_expr}, set({label})))'
        targets = [str(lbl) for lbl in run_query(rdeps_expr, cwd=repo_root, universe_scope=universe_scope)]
        t2_end = time.monotonic()

        t_total = time.monotonic() - t0

        print(f"step 1 (validate labels):    {t1_end - t1_start:6.2f}s")
        print(f"step 2 (rdeps query):        {t2_end - t2_start:6.2f}s")
        print(f"total:                       {t_total:6.2f}s")
        print(f"\naffected tests ({len(targets)}):")
        for t in sorted(targets):
            print(f"  {t}")

    finally:
        # Restore original file content.
        target_path.write_text(original)

    return 0


if __name__ == "__main__":
    sys.exit(main())

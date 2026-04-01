"""Pre-commit hook: verify affected Bazel tests are cached and passing.

Uses pygit2 for fast staged file discovery, then:
1. Converts staged files to candidate Bazel source file labels
2. Validates labels via kind("source file", ...) query
3. Finds affected test targets via rdeps query with scoped universe
4. Checks tests are up-to-date via --check_tests_up_to_date

Requires --remote_download_minimal (or --remote_download_toplevel) in .bazelrc
for --check_tests_up_to_date to work with RBE. Without this, test results only
exist in the remote cache and the check always reports "not up-to-date".
See https://github.com/bazelbuild/bazel/issues/3978 for details.

Currently set as `build:rbe --remote_download_minimal` in .bazelrc.

Guarded by DUCKTAPE_PRECOMMIT_ENFORCE_BAZEL_TESTS=1 (default off).
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from pathlib import Path

import pygit2

from util.bazel.workspace import BazelLabel, BazelWorkspace

# Infrastructure files that affect too many targets — CI catches these.
_INFRA_PATTERNS = (
    "MODULE.bazel",
    "MODULE.bazel.lock",
    "requirements_bazel.txt",
    ".bazelrc",
    ".bazelversion",
    "WORKSPACE",
    "WORKSPACE.bazel",
    "WORKSPACE.bzlmod",
)
_INFRA_GLOBS = ("devinfra/bazel*",)

# Bazel packages excluded from the query universe.
# These have external deps that fail at repo fetch time.
# TODO: make exclusions configurable (e.g. via .bazelproject or a config file)
# so they don't require rebuilding the Nix package, and/or auto-detect packages
# whose repo rules fail at fetch time and skip them gracefully.
_EXCLUDED_PACKAGES = {
    "gterm_theme"  # pycairo — requires dbus-1 system library not available everywhere
}

_PREFIX = "enforce-bazel-tests"


def _is_infra_file(path: str) -> bool:
    if path in _INFRA_PATTERNS:
        return True
    return any(fnmatch.fnmatch(path, g) for g in _INFRA_GLOBS)


def _get_staged_files(repo: pygit2.Repository) -> list[str]:
    """Get staged file paths using fast index-to-HEAD diff.

    Uses index.diff_to_tree(HEAD) instead of repo.status() — the latter
    triggers ~160k syscalls on 9p filesystems (~12s). diff_to_tree only
    compares the index to HEAD (~0.003s).
    """
    try:
        head_tree = repo.head.peel(pygit2.Tree)
    except pygit2.GitError:
        head_tree = None

    repo.index.read()
    if head_tree is not None:
        diff = repo.index.diff_to_tree(head_tree)
        return [delta.new_file.path for delta in diff.deltas]
    return [entry.path for entry in repo.index]


def _has_build_file(path: Path) -> bool:
    return (path / "BUILD.bazel").exists() or (path / "BUILD").exists()


def build_universe(repo_root: Path) -> list[str]:
    """Find Bazel package dirs for the query universe, excluding broken packages.

    Returns sorted list of Bazel package paths (relative to repo root, empty
    string for root). Top-level excluded packages are skipped entirely.
    For sub-package exclusions (e.g. "x/cotrl"), the parent is expanded into
    its sibling sub-packages so the broken one can be omitted.
    """
    top_level_excluded = {p for p in _EXCLUDED_PACKAGES if "/" not in p}
    sub_excluded = {p for p in _EXCLUDED_PACKAGES if "/" in p}
    # Parent dirs that need sub-package expansion (e.g. "x" for "x/cotrl").
    parents_to_expand = {p.split("/", 1)[0] for p in sub_excluded}

    dirs: list[str] = []
    for entry in sorted(repo_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "bazel-")):
            continue
        if entry.name in top_level_excluded:
            continue
        if entry.name in parents_to_expand:
            # Expand into individual sub-packages, skipping excluded ones.
            for sub in sorted(entry.iterdir()):
                if not sub.is_dir():
                    continue
                rel = f"{entry.name}/{sub.name}"
                if rel in sub_excluded:
                    continue
                if _has_build_file(sub):
                    dirs.append(rel)
            # Also include the parent itself if it has a BUILD file.
            if _has_build_file(entry):
                dirs.append(entry.name)
            continue
        if _has_build_file(entry):
            dirs.append(entry.name)

    if _has_build_file(repo_root):
        dirs.insert(0, "")
    return dirs


def find_affected_tests(
    workspace: BazelWorkspace, candidates: list[BazelLabel], *, timeout: int | None = None
) -> list[BazelLabel]:
    """Find test targets affected by the given source file label candidates.

    Two-step query:
    1. Validate candidates against known Bazel source files in their packages.
    2. Find test targets that transitively depend on the valid sources via
       rdeps with a scoped universe (excluding broken packages).
    """
    if not candidates:
        return []

    # Step 1: Validate labels — not all files in a Bazel package are source
    # targets (e.g. .pre-commit-config.yaml in the root package).
    packages = {label.package for label in candidates}
    pkg_union = " + ".join(f"//{pkg}:*" for pkg in sorted(str(p) if p != Path() else "" for p in packages))
    validate_expr = f'kind("source file", {pkg_union})'
    known_sources = set(workspace.query(validate_expr, timeout=timeout))

    valid_labels = [label for label in candidates if label in known_sources]
    if not valid_labels:
        return []

    # Step 2: Find affected test targets via rdeps with scoped universe.
    universe_dirs = build_universe(workspace.root)
    if not universe_dirs:
        return []

    parts: list[str] = []
    for d in universe_dirs:
        if d == "":
            # Root package — use //:all (not //:* which Bazel rejects as empty target name)
            parts.append("//:all")
        else:
            parts.append(f"//{d}/...")
    universe_expr = " + ".join(parts)
    universe_scope = ",".join(parts)

    labels_set = " ".join(str(label) for label in valid_labels)
    rdeps_expr = f'kind(".*_test", rdeps({universe_expr}, set({labels_set})))'
    return workspace.query(rdeps_expr, timeout=timeout, universe_scope=universe_scope)


def main() -> int:
    if os.environ.get("DUCKTAPE_PRECOMMIT_ENFORCE_BAZEL_TESTS") != "1":
        return 0

    repo = pygit2.Repository(".")
    repo_root = Path(repo.workdir).resolve()

    staged = _get_staged_files(repo)
    if not staged:
        return 0

    if any(_is_infra_file(f) for f in staged):
        print(f"{_PREFIX}: infrastructure file changed, skipping (CI catches these)")
        return 0

    workspace = BazelWorkspace(root=repo_root)
    candidates: list[BazelLabel] = []
    for f in staged:
        label = workspace.file_to_label(Path(f))
        if label is not None:
            candidates.append(label)

    if not candidates:
        return 0

    timeout = int(os.environ.get("DUCKTAPE_BAZEL_QUERY_TIMEOUT", "120"))

    try:
        affected = find_affected_tests(workspace, candidates, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"{_PREFIX}: bazel query timed out after {timeout}s", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"{_PREFIX}: bazel query failed (exit {e.returncode})", file=sys.stderr)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"{_PREFIX}: bazel not found on PATH", file=sys.stderr)
        return 1

    if not affected:
        return 0

    targets = [str(label) for label in affected]
    print(f"{_PREFIX}: checking {len(targets)} affected test(s)...")

    run_tests = os.environ.get("DUCKTAPE_PRECOMMIT_RUN_TESTS", "") == "1"

    if run_tests:
        print(f"{_PREFIX}: running tests (DUCKTAPE_PRECOMMIT_RUN_TESTS=1)...")

    try:
        rc = workspace.test(targets, check_up_to_date=not run_tests, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"{_PREFIX}: bazel test timed out after {timeout}s", file=sys.stderr)
        return 1

    if rc == 0:
        return 0

    if run_tests:
        print(f"{_PREFIX}: tests failed.", file=sys.stderr)
    else:
        print(f"{_PREFIX}: affected tests are not up-to-date or failing.", file=sys.stderr)
        print(f"Run: bazel test {' '.join(targets)}", file=sys.stderr)
        print("Or set DUCKTAPE_PRECOMMIT_RUN_TESTS=1 to run tests automatically.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

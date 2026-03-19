"""Benchmark: measure wall-clock time for Bazel test target discovery strategies.

Benchmarks from a cold Bazel server (shut down before each query).

Uses a separate --output_base with explicit startup flags (proxy + TLS CA)
so that cold-start benchmarks can run under ``bazel run`` without workspace
lock contention with the parent server.

Saves stdout/stderr to a timestamped output directory under
/tmp/enforce_bazel_tests_bench/.

Usage:
    bazel run //x/enforce_bazel_tests:bench
    bazel run //x/enforce_bazel_tests:bench -- --profile  # enable Bazel JSON profiles
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pygit2

from util.bazel.workspace import BazelWorkspace, get_build_workspace_directory
from x.enforce_bazel_tests.enforce_bazel_tests import build_universe

# The file we'll temporarily modify to simulate a change.
_TARGET_FILE = Path("util/bazel/workspace.py")
_SENTINEL = "\n# benchmark-sentinel-comment\n"
_QUERY_TIMEOUT = 300

# Separate output base to avoid lock contention with the parent `bazel run`.
_BENCH_OUTPUT_BASE = Path("/tmp/enforce_bazel_tests_bench/output_base")


def _assert_index_clean(repo: pygit2.Repository) -> None:
    """Raise if the index (staging area) has staged changes."""
    staged = {
        path: flags
        for path, flags in repo.status().items()
        if flags & (pygit2.GIT_STATUS_INDEX_NEW | pygit2.GIT_STATUS_INDEX_MODIFIED | pygit2.GIT_STATUS_INDEX_DELETED)
    }
    if staged:
        raise RuntimeError(f"git index is not clean ({len(staged)} staged files). Commit or stash first.")


def _read_session_startup_flags() -> tuple[str, ...]:
    """Extract JVM startup flags from the session bazelrc.

    Re-injects the session's proxy and TLS CA JVM args so the bench's
    separate Bazel server can reach BCR through the auth proxy.
    """
    path_str = os.environ.get("SESSION_BAZELRC")
    if path_str is None:
        return ()
    p = Path(path_str)
    if not p.exists():
        return ()
    flags = []
    for raw_line in p.read_text().splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("startup --host_jvm_args="):
            flags.append(stripped.removeprefix("startup "))
    return tuple(flags)


def _bench_query(
    ws: BazelWorkspace, name: str, expr: str, *, cold: bool, out_dir: Path, profile: bool, run_index: int
) -> None:
    """Run a query, save output, print timing."""
    if cold:
        ws.shutdown()

    query_dir = out_dir / f"{run_index:02d}_{name}"
    query_dir.mkdir(parents=True, exist_ok=True)
    profile_path = (query_dir / "profile.json") if profile else None

    t0 = time.monotonic()
    try:
        result = ws.query(expr, persist_dir=query_dir, timeout=_QUERY_TIMEOUT, profile_path=profile_path)
    except subprocess.CalledProcessError as e:
        elapsed = time.monotonic() - t0
        print(f"  {name + ':':.<40s} {elapsed:6.2f}s  FAILED (exit {e.returncode})")
        if e.stderr:
            # Show last meaningful line of stderr
            for line in reversed(e.stderr.strip().splitlines()):
                if line.strip() and not line.strip().startswith("Loading:"):
                    print(f"    {line.strip()[:200]}")
                    break
        (query_dir / "elapsed_s.txt").write_text(f"{elapsed:.3f}")
        return
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        print(f"  {name + ':':.<40s} {elapsed:6.2f}s  TIMEOUT")
        (query_dir / "elapsed_s.txt").write_text(f"{elapsed:.3f}")
        return

    elapsed = time.monotonic() - t0
    print(f"  {name + ':':.<40s} {elapsed:6.2f}s  ({len(result)} targets)")
    (query_dir / "elapsed_s.txt").write_text(f"{elapsed:.3f}")
    (query_dir / "targets.txt").write_text("\n".join(str(lbl) for lbl in result) + "\n")


def _universe_expr(repo_root: Path) -> str:
    """Build a scoped universe expression excluding broken packages."""
    dirs = build_universe(repo_root)
    parts: list[str] = []
    for d in dirs:
        if d == "":
            parts.append("//:all")
        else:
            parts.append(f"//{d}/...")
    return " + ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Bazel test discovery strategies")
    parser.add_argument("--profile", action="store_true", help="Enable Bazel JSON trace profiles")
    args = parser.parse_args()

    repo_root = get_build_workspace_directory()
    repo = pygit2.Repository(str(repo_root))
    _assert_index_clean(repo)

    _BENCH_OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    # Main workspace (default output base) for file_to_label.
    main_ws = BazelWorkspace(root=repo_root)
    label = main_ws.file_to_label(_TARGET_FILE)
    if label is None:
        raise ValueError(f"No BUILD file found for {_TARGET_FILE}")

    # Bench workspace with separate output base to avoid lock contention.
    bench_ws = BazelWorkspace(
        root=repo_root, output_base=_BENCH_OUTPUT_BASE, startup_flags=_read_session_startup_flags()
    )

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(f"/tmp/enforce_bazel_tests_bench/runs/{timestamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    universe = _universe_expr(repo_root)

    print(f"target file:  {_TARGET_FILE}")
    print(f"bazel label:  {label}")
    print(f"output base:  {_BENCH_OUTPUT_BASE}")
    print(f"results dir:  {out_dir}")
    if args.profile:
        print("profiling:    enabled (JSON trace profiles)")
    print()

    run_index = 0

    # --- Section 1: kind/tests queries (each from cold) ---
    print("=== kind/tests queries (each from cold start) ===")
    cold_kind_cases = [
        ("kind_py_test_all", 'kind("py_test", //...)'),
        ("kind_go_test_all", 'kind("go_test", //...)'),
        ("kind_any_test_all", 'kind(".*_test", //...)'),
        ("tests_all", "tests(//...)"),
        ("kind_py_test_scoped", f'kind("py_test", {universe})'),
        ("kind_any_test_scoped", f'kind(".*_test", {universe})'),
        ("tests_scoped", f"tests({universe})"),
    ]
    for name, expr in cold_kind_cases:
        _bench_query(bench_ws, name, expr, cold=True, out_dir=out_dir, profile=args.profile, run_index=run_index)
        run_index += 1

    # --- Section 2: alternative strategies (each from cold) ---
    print("\n=== alternative strategies (each from cold start) ===")
    cold_alt_cases = [
        ("enumerate_all", "//..."),
        ("rdeps_all", f"rdeps(//..., {label})"),
        ("somepath_all", f'somepath(kind(".*_test", //...), {label})'),
        ("allrdeps", f'kind(".*_test", allrdeps({label}))'),
    ]
    for name, expr in cold_alt_cases:
        _bench_query(bench_ws, name, expr, cold=True, out_dir=out_dir, profile=args.profile, run_index=run_index)
        run_index += 1

    # --- Section 3: warm-server queries ---
    print("\n=== warm-server queries (no shutdown between queries) ===")
    # Server is warm from the last query above. If all cold queries failed,
    # start a server with a trivial query first.
    with contextlib.suppress(subprocess.CalledProcessError):
        bench_ws.query("//util/bazel:workspace.py", timeout=_QUERY_TIMEOUT)

    warm_cases = [
        ("warm_kind_py_test", 'kind("py_test", //...)'),
        ("warm_kind_any_test", 'kind(".*_test", //...)'),
        ("warm_tests", "tests(//...)"),
        ("warm_rdeps", f"rdeps(//..., {label})"),
        ("warm_allrdeps", f'kind(".*_test", allrdeps({label}))'),
    ]
    for name, expr in warm_cases:
        _bench_query(bench_ws, name, expr, cold=False, out_dir=out_dir, profile=args.profile, run_index=run_index)
        run_index += 1

    print(f"\nResults saved to: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

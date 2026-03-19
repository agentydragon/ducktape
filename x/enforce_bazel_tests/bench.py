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
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pygit2

from util.bazel.workspace import BazelLabel, BazelWorkspace, get_build_workspace_directory
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


def _bazel_cmd(*, profile_path: Path | None = None) -> list[str]:
    """Build the base bazel command with output_base and startup flags.

    Re-injects the session's proxy and TLS CA JVM args so the bench's
    separate Bazel server can reach BCR through the auth proxy.
    """
    cmd = ["bazel", f"--output_base={_BENCH_OUTPUT_BASE}"]

    # Propagate the session's proxy and TLS startup flags if available.
    session_bazelrc = _find_session_bazelrc()
    if session_bazelrc is not None:
        for raw_line in session_bazelrc.read_text().splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("startup --host_jvm_args="):
                cmd.append(stripped.removeprefix("startup "))
    return cmd


def _find_session_bazelrc() -> Path | None:
    """Find the session bazelrc via SESSION_BAZELRC env var."""
    path_str = os.environ.get("SESSION_BAZELRC")
    if path_str is not None:
        p = Path(path_str)
        if p.exists():
            return p
    return None


def _shutdown() -> None:
    """Shut down the bench's Bazel server."""
    cmd = [*_bazel_cmd(), "shutdown"]
    subprocess.run(cmd, check=False, capture_output=True)


def _query(expr: str, *, repo_root: Path, persist_dir: Path | None = None, profile: bool = False) -> list[BazelLabel]:
    """Run a bazel query with the bench's output base."""
    cmd = [*_bazel_cmd(), "query", "--output=label"]
    if profile and persist_dir is not None:
        profile_path = persist_dir / "profile.json"
        cmd.extend([f"--profile={profile_path}", "--generate_json_trace_profile"])

    with tempfile.NamedTemporaryFile(mode="w", suffix=".bazelquery") as qf:
        qf.write(expr)
        qf.flush()
        cmd.append(f"--query_file={qf.name}")
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_root, check=False, timeout=_QUERY_TIMEOUT)

    if persist_dir is not None:
        (persist_dir / "query.txt").write_text(expr)
        (persist_dir / "stdout").write_text(result.stdout)
        (persist_dir / "stderr").write_text(result.stderr)
        (persist_dir / "exit_code").write_text(str(result.returncode))

    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, "bazel", result.stdout, result.stderr)
    return [BazelLabel.parse(line) for line in result.stdout.splitlines() if line]


def _bench_query(
    name: str, expr: str, *, cold: bool, repo_root: Path, out_dir: Path, profile: bool, run_index: int
) -> None:
    """Run a query, save output, print timing."""
    if cold:
        _shutdown()

    query_dir = out_dir / f"{run_index:02d}_{name}"
    query_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    try:
        result = _query(expr, repo_root=repo_root, persist_dir=query_dir, profile=profile)
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

    ws = BazelWorkspace(root=repo_root)
    label = ws.file_to_label(_TARGET_FILE)
    if label is None:
        raise ValueError(f"No BUILD file found for {_TARGET_FILE}")

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
        _bench_query(
            name, expr, cold=True, repo_root=repo_root, out_dir=out_dir, profile=args.profile, run_index=run_index
        )
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
        _bench_query(
            name, expr, cold=True, repo_root=repo_root, out_dir=out_dir, profile=args.profile, run_index=run_index
        )
        run_index += 1

    # --- Section 3: warm-server queries ---
    print("\n=== warm-server queries (no shutdown between queries) ===")
    # Server is warm from the last query above. If all cold queries failed,
    # start a server with a trivial query first.
    with contextlib.suppress(subprocess.CalledProcessError):
        _query("//util/bazel:workspace.py", repo_root=repo_root)

    warm_cases = [
        ("warm_kind_py_test", 'kind("py_test", //...)'),
        ("warm_kind_any_test", 'kind(".*_test", //...)'),
        ("warm_tests", "tests(//...)"),
        ("warm_rdeps", f"rdeps(//..., {label})"),
        ("warm_allrdeps", f'kind(".*_test", allrdeps({label}))'),
    ]
    for name, expr in warm_cases:
        _bench_query(
            name, expr, cold=False, repo_root=repo_root, out_dir=out_dir, profile=args.profile, run_index=run_index
        )
        run_index += 1

    print(f"\nResults saved to: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

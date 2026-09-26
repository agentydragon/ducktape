#!/usr/bin/env python3
"""Run the repository's Bazel CI test and build gate.

This file is executed by ``bb remote --script`` on a BuildBuddy runner VM.  It
does not inherit the GitHub Actions environment, so values needed to identify
the source revision and BuildBuddy invocations are forwarded explicitly by the
workflow.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import tempfile
from pathlib import Path


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command with its output connected to the CI log."""
    return subprocess.run(args, check=check, text=True)


def _capture(args: list[str]) -> str:
    """Run a command, preserving stderr in the CI log and returning stdout."""
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, text=True).stdout


def _git_output(*args: str) -> str:
    return _capture(["git", *args]).strip()


def graph_wide_change_reason(paths: list[str]) -> str | None:
    """Return the first reason a changed path requires a graph-wide sweep."""
    exact_paths = {
        # keep-sorted start
        ".bazelignore",
        ".bazelrc",
        ".bazelversion",
        ".github/workflows/bazel-ci.yml",
        ".github/workflows/ci.yml",
        "Cargo.lock",
        "Cargo.toml",
        "MODULE.bazel",
        "MODULE.bazel.lock",
        "Pipfile.lock",
        "WORKSPACE",
        "WORKSPACE.bazel",
        "WORKSPACE.bzlmod",
        "WORKSPACE.bzlmod.lock",
        "devinfra/ci/bazel_ci.py",
        "devinfra/ci/bazel_ci.sh",
        "go.mod",
        "go.sum",
        "package.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "uv.lock",
        # keep-sorted end
    }
    manifest_basenames = {
        # keep-sorted start
        "Cargo.lock",
        "Cargo.toml",
        "MODULE.bazel",
        "MODULE.bazel.lock",
        "Pipfile.lock",
        "WORKSPACE",
        "WORKSPACE.bazel",
        "WORKSPACE.bzlmod",
        "WORKSPACE.bzlmod.lock",
        "go.mod",
        "go.sum",
        "package.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "uv.lock",
        # keep-sorted end
    }

    for path in paths:
        basename = Path(path).name
        if path in exact_paths or basename in manifest_basenames:
            return path
        if fnmatch.fnmatch(path, "*.bzl"):
            return path
        if basename.startswith("requirements") and basename.endswith(".txt"):
            return path
    return None


def _quote_labels(path: Path) -> str:
    """Format labels as literals in a Bazel ``set()`` expression."""
    labels = [line for line in path.read_text().splitlines() if line]
    return " ".join(f'"{label}"' for label in labels)


def _probe(stage: str) -> None:
    """Record runner state, while keeping the probe best-effort."""
    _run(["python3", "devinfra/ci/bb_runner_probe.py", "snapshot", stage], check=False)


def _finalize_probe() -> None:
    _run(["python3", "devinfra/ci/bb_runner_probe.py", "finalize", "--upload"], check=False)


def _announce_bazel_command(role: str) -> None:
    """Mark a Bazel command for profile analysis without exposing its invocation ID."""
    print(f"CI_BAZEL_COMMAND role={role}", flush=True)


def _rbe_flags() -> list[str]:
    flags = ["--config=rbe", "--config=ci"]
    if os.environ.get("GITHUB_EVENT_NAME") == "push":
        # Only non-PR workflow pushes should enter BuildBuddy's mainline target
        # tracker. PR and hosted-script invocations must remain untracked.
        flags.extend(["--build_metadata=ROLE=CI", "--build_metadata=DISABLE_TARGET_TRACKING=false"])
    flags.append("--remote_default_exec_properties=container-image=docker://" + os.environ["RBE_CONTAINER_IMAGE"])
    return flags


def _select_targets(temp_dir: Path) -> tuple[bool, list[str], Path]:
    """Select full or bazel-diff affected targets and write the test query."""
    test_query = temp_dir / "test-query.txt"
    pr_head_sha = os.environ.get("PR_HEAD_SHA", "")
    if not pr_head_sha:
        test_query.write_text("tests(//...)\n")
        return True, ["//..."], test_query

    head_sha = _git_output("rev-parse", "HEAD")
    _run(["git", "fetch", "--depth=2", "--no-tags", "origin", head_sha])
    try:
        base = _git_output("rev-parse", f"{head_sha}^1")
        pr_head = _git_output("rev-parse", f"{head_sha}^2")
    except subprocess.CalledProcessError:
        print("::error::pull_request CI expected github.sha to be a two-parent synthetic merge commit")
        raise SystemExit(1) from None

    if pr_head != pr_head_sha:
        print(f"::error::synthetic merge head parent ({pr_head}) != trusted PR head ({pr_head_sha})")
        raise SystemExit(1)
    event_base = os.environ.get("PR_BASE_SHA", "")
    if event_base and base != event_base:
        print(
            f"::notice::base advanced since the PR event (event base {event_base} -> merge base {base}); "
            "diffing against the merge's base parent, which is correct"
        )
    print(f"before-revision: {base}")
    print(f"pr-head:         {pr_head}")
    print(f"merge:           {head_sha}")

    changed_files = temp_dir / "changed-files.txt"
    changed_files.write_text(_capture(["git", "diff", "--name-only", base, head_sha]))
    reason = graph_wide_change_reason(changed_files.read_text().splitlines())
    if reason is not None:
        print(f"graph-wide change: {reason}")
        test_query.write_text("tests(//...)\n")
        return True, ["//..."], test_query

    cache_dir = temp_dir / "bd-cache"
    cache_dir.mkdir()
    base_hashes = cache_dir / "base.json"
    head_hashes = cache_dir / "head.json"
    affected_raw = temp_dir / "affected-raw.txt"
    affected_query = temp_dir / "affected-query.txt"
    affected = temp_dir / "affected.txt"

    _run(["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", base])
    _run(["bazel-diff", "generate-hashes", "-w", str(Path.cwd()), "-b", "bazel", str(base_hashes)])
    _run(["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", head_sha])
    _run(["bazel-diff", "generate-hashes", "-w", str(Path.cwd()), "-b", "bazel", str(head_hashes)])
    affected_raw.write_text(
        _capture(
            [
                "bazel-diff",
                "get-impacted-targets",
                "-w",
                str(Path.cwd()),
                "-sh",
                str(base_hashes),
                "-fh",
                str(head_hashes),
            ]
        )
    )
    raw_labels = _quote_labels(affected_raw)
    affected_query.write_text(
        f'set({raw_labels}) except kind("source file", set({raw_labels})) '
        f'except attr("tags", "manual", set({raw_labels}))\n'
    )
    affected.write_text(_capture(["bazel", "query", f"--query_file={affected_query}"]))

    target_count = len(affected.read_text().splitlines())
    print(f"affected targets: {target_count}")
    if target_count == 0:
        print("No targets affected by this PR — skipping test/build.")
        raise SystemExit(0)

    labels = _quote_labels(affected)
    test_query.write_text(f"tests(set({labels}))\n")
    return False, [f"--target_pattern_file={affected}"], test_query


def main() -> int:
    """Run selection, test, and build, then finalize runner diagnostics."""
    with tempfile.TemporaryDirectory(prefix="ducktape-bazel-ci-") as temp_path:
        temp_dir = Path(temp_path)
        try:
            full_sweep, targets, test_query = _select_targets(temp_dir)
            test_target_count = len(_capture(["bazel", "query", f"--query_file={test_query}"]).splitlines())
            print(f"test targets in scope: {test_target_count}")

            _probe("before-test")
            _run(["bazel", "shutdown"], check=False)
            if test_target_count == 0:
                print("No test targets in scope -- skipping bazel test.")
                _announce_bazel_command("build")
                _run(
                    [
                        "bazel",
                        "build",
                        f"--invocation_id={os.environ['BUILD_INVOCATION_ID']}",
                        "--keep_going",
                        *_rbe_flags(),
                        *targets,
                    ]
                )
                _probe("after-build")
                return 0

            _announce_bazel_command("test")
            test_result = _run(
                [
                    "bazel",
                    "test",
                    f"--invocation_id={os.environ['TEST_INVOCATION_ID']}",
                    "--keep_going",
                    *_rbe_flags(),
                    *targets,
                ],
                check=False,
            )
            test_rc = test_result.returncode
            if test_rc == 4 and not full_sweep:
                print("No runnable tests after tag filtering in the affected set; continuing to build.")
                test_rc = 0
            if test_rc != 0:
                return test_rc

            _probe("after-test")
            _announce_bazel_command("build")
            _run(
                [
                    "bazel",
                    "build",
                    f"--invocation_id={os.environ['BUILD_INVOCATION_ID']}",
                    "--keep_going",
                    *_rbe_flags(),
                    *targets,
                ]
            )
            _probe("after-build")
            return 0
        finally:
            _finalize_probe()


if __name__ == "__main__":
    raise SystemExit(main())

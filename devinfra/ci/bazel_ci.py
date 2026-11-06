#!/usr/bin/env python3
"""Run the repository's Bazel CI test and build gate.

This file is executed by ``bb remote --script`` on a BuildBuddy runner VM.  It
does not inherit the GitHub Actions environment, so values needed to identify
the source revision and BuildBuddy invocations are forwarded explicitly by the
workflow.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


def _read_json_lockfile(path: Path, *, description: str) -> bytes | None:
    """Read a lockfile if it is readable JSON, without interpreting Bazel's schema."""
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s Bazel module lockfile %s: %s", description, path, exc)
        return None

    try:
        json.loads(contents)
    except (ValueError, RecursionError) as exc:
        logger.warning("Ignoring invalid JSON in %s Bazel module lockfile %s: %s", description, path, exc)
        return None
    return contents


def _atomic_write(path: Path, contents: bytes) -> None:
    """Write through a temporary sibling directory so replacement is atomic."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bazel-module-lock-", dir=path.parent) as temp_dir:
        temporary_path = Path(temp_dir) / path.name
        temporary_path.write_bytes(contents)
        temporary_path.replace(path)


@contextmanager
def _module_lockfile_cache() -> Iterator[None]:
    """Restore and persist Bazel's disposable module-resolution lockfile cache."""
    workspace_lockfile = Path.cwd() / "MODULE.bazel.lock"
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        cache_root: Path | None = Path(cache_home)
    else:
        try:
            cache_root = Path.home() / ".cache"
        except (OSError, RuntimeError) as exc:
            logger.warning("Could not locate Bazel module lockfile cache: %s", exc)
            cache_root = None
    cache_lockfile = cache_root / "ducktape" / "bazel" / "MODULE.bazel.lock" if cache_root is not None else None

    if cache_lockfile is not None and not workspace_lockfile.exists():
        cached_contents = _read_json_lockfile(cache_lockfile, description="cached")
        if cached_contents is not None:
            try:
                _atomic_write(workspace_lockfile, cached_contents)
            except (OSError, ValueError) as exc:
                logger.warning("Could not restore cached Bazel module lockfile %s: %s", cache_lockfile, exc)

    try:
        yield
    finally:
        if cache_lockfile is not None:
            workspace_contents = _read_json_lockfile(workspace_lockfile, description="workspace")
            if workspace_contents is not None:
                try:
                    _atomic_write(cache_lockfile, workspace_contents)
                except (OSError, ValueError) as exc:
                    logger.warning("Could not save Bazel module lockfile cache %s: %s", cache_lockfile, exc)


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
        if path in exact_paths or basename in manifest_basenames or basename.endswith(".MODULE.bazel"):
            return path
        if fnmatch.fnmatch(path, "*.bzl"):
            # Keep this fallback for the gaps in debug/bazel_diff_starlark.md.
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
    _run(["python3", "devinfra/buildbuddy/ci/runner_probe.py", "snapshot", stage], check=False)


def _finalize_probe() -> None:
    _run(["python3", "devinfra/buildbuddy/ci/runner_probe.py", "finalize", "--upload"], check=False)


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


def _run_bazel(
    command: str, *, role: str, targets: list[str], extra_flags: tuple[str, ...] = (), check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run one named CI phase with its common Bazel flags."""
    _announce_bazel_command(role)
    return _run(
        [
            "bazel",
            command,
            f"--invocation_id={os.environ[f'{role.upper()}_INVOCATION_ID']}",
            "--keep_going",
            *_rbe_flags(),
            *extra_flags,
            *targets,
        ],
        check=check,
    )


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

    # Cross-run base-hash caching was evaluated and deferred; see
    # debug/bazel_diff_base_hash_cache.md for measurements and revisit conditions.
    # Base/head queries share warm Bazel state: time the whole driver, not just base.
    # Query flattens scalar select() values; fingerprint the raw attributes too.
    # Remaining omissions are documented in debug/bazel_diff_starlark.md.
    hash_command = [
        "bazel-diff",
        "generate-hashes",
        "-co=--proto:include_synthetic_attribute_hash",
        "-w",
        str(Path.cwd()),
        "-b",
        "bazel",
    ]
    _run(["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", base])
    _run([*hash_command, str(base_hashes)])
    _run(["git", "-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", head_sha])
    _run([*hash_command, str(head_hashes)])
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
    """Run selection, race-enabled tests and normal builds, then finalize runner diagnostics."""
    with _module_lockfile_cache(), tempfile.TemporaryDirectory(prefix="ducktape-bazel-ci-") as temp_path:
        temp_dir = Path(temp_path)
        try:
            full_sweep, targets, test_query = _select_targets(temp_dir)
            test_target_count = len(_capture(["bazel", "query", f"--query_file={test_query}"]).splitlines())
            print(f"test targets in scope: {test_target_count}")

            _probe("before-test")
            if test_target_count == 0:
                print("No test targets in scope -- skipping bazel test.")
            else:
                test_rc = _run_bazel("test", role="test", targets=targets, check=False).returncode
                if test_rc == 4 and not full_sweep:
                    print("No runnable tests after tag filtering in the affected set; continuing to build.")
                    test_rc = 0
                if test_rc != 0:
                    return test_rc
                _probe("after-test")

            _run_bazel("build", role="build", targets=targets)
            _probe("after-build")
            return 0
        finally:
            _finalize_probe()


if __name__ == "__main__":
    raise SystemExit(main())

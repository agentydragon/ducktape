"""Git hook entry points for pre-commit framework.

Installed as separate console scripts via the claude-hooks wheel:
- ducktape-precommit: file validations (filenames, cluster, frozen-specimens)
- ducktape-pytest-main-check: verify test files have pytest_bazel.main() entry points
- ducktape-prepare-commit-msg: block amending already-pushed commits
- ducktape-commit-msg: enforce BAZEL_TEST_INVOCATIONS= tag
- ducktape-enforce-bazel-tests: verify affected Bazel tests are cached/passing
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable
from pathlib import Path

import pygit2
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import StatusCode

from cluster.validation.validate_all import validate as validate_cluster
from devinfra.precommit.commit_tag import TestTagError, check_commit_message
from devinfra.precommit.enforce_bazel_tests.enforce_bazel_tests import run as enforce_bazel_tests_run
from devinfra.precommit.filename_conventions import check_filename_conventions
from devinfra.precommit.frozen_specimens import check_specimen_code_changes
from devinfra.pytest_main import BazelPyTestIndex, build_bazel_index, check_files_async
from util.bazel.workspace import BazelWorkspace, detect_bazel_backend
from util.otel import JsonlSpanExporter

_IGNORE_ATTRS = ("linguist-generated", "gitlab-generated", "rules-lint-ignored", "filename-conventions-ignored")

tracer = trace.get_tracer(__name__)


def _is_ignored(repo: pygit2.Repository, path: str) -> bool:
    return any(repo.get_attr(path, a) in (True, "true") for a in _IGNORE_ATTRS)


def is_cluster_validated(p: Path) -> bool:
    if p.is_relative_to("cluster/k8s") and p.suffix in (".yaml", ".yml"):
        return True
    return p.is_relative_to("cluster/terraform") and "cilium" in p.parts


async def run_pytest_main_check(files: list[Path], repo_root: Path, bazel_index: BazelPyTestIndex) -> str | None:
    """Check that test files have pytest_bazel.main() calls."""
    if not files:
        candidates = [p.relative_to(repo_root) for p in bazel_index.known_srcs]
    else:
        candidates = [f for f in files if (repo_root / f).resolve() in bazel_index.known_srcs]

    test_files = [f for f in candidates if f.name != "conftest.py"]
    if not test_files:
        return None

    results = await check_files_async(test_files, repo_root, bazel_index)
    failed = [r for r in results if not r.passed]
    if failed:
        return "\n".join(f"{r.file_path}: {r.reason}" for r in failed)
    return None


async def run_cluster_validate(files: list[Path], repo_root: Path) -> str | None:
    if not any(is_cluster_validated(f) for f in files):
        return None
    errors = await validate_cluster(repo_root / "cluster/k8s", skip_flux_build=True)
    if errors:
        return "\n".join(f"  {e.strip()}" for e in errors)
    return None


async def run_filename_convention_check(deltas: list[pygit2.DiffDelta], head_tree: pygit2.Tree | None) -> str | None:
    violations = check_filename_conventions(deltas, head_tree)
    return "\n".join(violations) if violations else None


async def run_frozen_specimens_check(deltas: list[pygit2.DiffDelta], head_tree: pygit2.Tree | None) -> str | None:
    violations = check_specimen_code_changes(deltas, head_tree)
    if violations:
        return "Changes to code/ in committed snapshots are not allowed.\n" + "\n".join(f"  {v}" for v in violations)
    return None


def get_all_files(repo: pygit2.Repository) -> list[Path]:
    """Get all tracked files from git index, excluding deleted files."""
    repo_root = Path(repo.workdir)
    return [Path(entry.path) for entry in repo.index if (repo_root / entry.path).exists()]


def _staged_deltas(repo: pygit2.Repository) -> tuple[pygit2.Tree | None, list[pygit2.DiffDelta]]:
    """Return (head_tree, staged deltas) for the current index."""
    if repo.head_is_unborn:
        head_tree = None
        base = repo[repo.TreeBuilder().write()].peel(pygit2.Tree)
    else:
        head_tree = repo.head.peel(pygit2.Tree)
        base = head_tree
    repo.index.read()
    return head_tree, list(repo.index.diff_to_tree(base).deltas)


def _setup_tracing(repo: pygit2.Repository) -> None:
    provider = TracerProvider()
    exporter = JsonlSpanExporter(Path(repo.path) / "precommit-traces.jsonl")
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


# ---------------------------------------------------------------------------
# Stage: pre-commit
# ---------------------------------------------------------------------------


async def _run_pre_commit(argv: list[str]) -> int:
    repo = pygit2.Repository(".")
    repo_root = Path(repo.workdir)
    _setup_tracing(repo)

    with tracer.start_as_current_span("precommit"):
        head_tree, all_deltas = _staged_deltas(repo)
        deltas = [d for d in all_deltas if not _is_ignored(repo, d.new_file.path)]

        files = [Path(f) for f in argv] if argv else get_all_files(repo)

        async def _traced(name: str, coro: Awaitable[str | None]) -> tuple[str, str | None]:
            with tracer.start_as_current_span(name):
                error = await coro
                if error:
                    trace.get_current_span().set_status(StatusCode.ERROR, error[:200])
                return (name, error)

        print(f"Validating {len(files)} files...")
        results = list(
            await asyncio.gather(
                _traced("filename-conventions", run_filename_convention_check(deltas, head_tree)),
                _traced("cluster-validate", run_cluster_validate(files, repo_root)),
                _traced("frozen-specimens", run_frozen_specimens_check(deltas, head_tree)),
            )
        )

        failed = []
        for name, error in results:
            if error:
                print(f"  {name}: FAILED")
                print(error, file=sys.stderr)
                failed.append(name)
            else:
                print(f"  {name}: ok")

    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Stage: prepare-commit-msg — block amending already-pushed commits
# ---------------------------------------------------------------------------


def _head_on_remote(repo: pygit2.Repository) -> bool:
    """True if HEAD is reachable from any remote branch (i.e., already pushed)."""
    head_oid = repo.head.target
    return any(
        ref.resolve().target == head_oid or repo.descendant_of(ref.resolve().target, head_oid)
        for ref in repo.references.objects
        if ref.name.startswith("refs/remotes/")
    )


def _run_prepare_commit_msg(argv: list[str]) -> int:
    # Git passes: <msg-file> <source> [<sha>]
    # source is "commit" for --amend
    source = argv[1] if len(argv) > 1 else ""
    if source != "commit":
        return 0

    repo = pygit2.Repository(".")
    if _head_on_remote(repo):
        print("ERROR: Refusing to amend a commit that has already been pushed.", file=sys.stderr)
        print(
            'Create a new commit instead. See AGENTS.md: "NEVER amend a commit that has already been pushed."',
            file=sys.stderr,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# Stage: commit-msg — enforce BAZEL_TEST_INVOCATIONS= tag
# ---------------------------------------------------------------------------

_TEST_TAG_ENV_VAR = "DUCKTAPE_PRECOMMIT_ENFORCE_TEST_TAG"

# TODO: Re-enable by default once pytest_main_check is faster.
_PYTEST_MAIN_CHECK_ENV_VAR = "DUCKTAPE_PYTEST_MAIN_CHECK"


def _run_commit_msg(argv: list[str]) -> int:
    if os.environ.get(_TEST_TAG_ENV_VAR) not in ("1", "true"):
        return 0

    if not argv:
        print("ERROR: commit message file path required as argument", file=sys.stderr)
        return 1

    message = Path(argv[0]).read_text()
    try:
        check_commit_message(message)
    except TestTagError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def main_pre_commit() -> int:
    return asyncio.run(_run_pre_commit(sys.argv[1:]))


def main_pytest_main_check() -> int:
    if os.environ.get(_PYTEST_MAIN_CHECK_ENV_VAR) not in ("1", "true"):
        return 0

    repo = pygit2.Repository(".")
    repo_root = Path(repo.workdir)
    _setup_tracing(repo)

    workspace = BazelWorkspace(root=repo_root, backend=detect_bazel_backend())
    bazel_index = build_bazel_index(workspace)

    files = [Path(f) for f in sys.argv[1:]] if sys.argv[1:] else get_all_files(repo)
    error = asyncio.run(run_pytest_main_check(files, repo_root, bazel_index))
    if error:
        print(error, file=sys.stderr)
        return 1
    return 0


def main_enforce_bazel_tests() -> None:
    if os.environ.get("DUCKTAPE_PRECOMMIT_ENFORCE_BAZEL_TESTS") not in ("1", "true"):
        return

    repo = pygit2.Repository(".")
    repo_root = Path(repo.workdir)
    workspace = BazelWorkspace(root=repo_root, backend=detect_bazel_backend())
    _, deltas = _staged_deltas(repo)

    enforce_bazel_tests_run(workspace, deltas)


def main_prepare_commit_msg() -> int:
    return _run_prepare_commit_msg(sys.argv[1:])


def main_commit_msg() -> int:
    return _run_commit_msg(sys.argv[1:])

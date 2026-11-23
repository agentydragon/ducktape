"""Git hook entry points for pre-commit framework.

Installed as separate console scripts via the ducktape-git-hooks wheel:
- ducktape-precommit: file validations (filenames, frozen-specimens)
- ducktape-prepare-commit-msg: block amending already-pushed commits
"""

from __future__ import annotations

import sys
from pathlib import Path

import pygit2
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import StatusCode

from devinfra.precommit.filename_conventions import check_filename_conventions
from devinfra.precommit.frozen_specimens import check_specimen_code_changes
from util.otel import JsonlSpanExporter

_IGNORE_ATTRS = ("linguist-generated", "gitlab-generated", "rules-lint-ignored", "filename-conventions-ignored")

tracer = trace.get_tracer(__name__)


def _is_ignored(repo: pygit2.Repository, path: str) -> bool:
    return any(repo.get_attr(path, a) in (True, "true") for a in _IGNORE_ATTRS)


def _filename_convention_error(deltas: list[pygit2.DiffDelta], head_tree: pygit2.Tree | None) -> str | None:
    violations = check_filename_conventions(deltas, head_tree)
    return "\n".join(violations) if violations else None


def _frozen_specimens_error(deltas: list[pygit2.DiffDelta], head_tree: pygit2.Tree | None) -> str | None:
    violations = check_specimen_code_changes(deltas, head_tree)
    if violations:
        return "Changes to code/ in committed snapshots are not allowed.\n" + "\n".join(f"  {v}" for v in violations)
    return None


def _staged_deltas(repo: pygit2.Repository) -> tuple[pygit2.Tree | None, list[pygit2.DiffDelta]]:
    """Return (head_tree, staged deltas) for the current index."""
    if repo.head_is_unborn:
        head_tree = None
        base = repo[repo.TreeBuilder().write()].peel(pygit2.Tree)
    else:
        head_tree = repo.head.peel(pygit2.Tree)
        base = head_tree
    # Index vs HEAD only: `repo.status()` also scans the working tree and is much slower.
    repo.index.read()
    return head_tree, list(repo.index.diff_to_tree(base).deltas)


def _setup_tracing(repo: pygit2.Repository) -> None:
    provider = TracerProvider()
    exporter = JsonlSpanExporter(Path(repo.path) / "precommit-traces.jsonl")
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def main_pre_commit() -> int:
    repo = pygit2.Repository(".")
    _setup_tracing(repo)

    with tracer.start_as_current_span("precommit"):
        head_tree, all_deltas = _staged_deltas(repo)
        deltas = [d for d in all_deltas if not _is_ignored(repo, d.new_file.path)]

        print(f"Validating {len(deltas)} changed files...")
        failed = False
        for name, check in (
            ("filename-conventions", _filename_convention_error),
            ("frozen-specimens", _frozen_specimens_error),
        ):
            with tracer.start_as_current_span(name) as span:
                error = check(deltas, head_tree)
                if error:
                    span.set_status(StatusCode.ERROR, error[:200])
            if error:
                print(f"  {name}: FAILED")
                print(error, file=sys.stderr)
                failed = True
            else:
                print(f"  {name}: ok")

    return int(failed)


# prepare-commit-msg stage: block amending already-pushed commits
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


def main_prepare_commit_msg() -> int:
    return _run_prepare_commit_msg(sys.argv[1:])

"""`workspace-gc`: reclaim local development state — worktrees, branches, Bazel output bases,
and Bazel's repo contents cache.

The domains are coupled (a branch checked out in a live worktree can't be deleted; an output
base orphans when its workspace worktree is removed, and a base for a workspace under `/tmp`
can be reclaimed without removing its workspace; a repo-cache entry is unreferenced once the
bases linking to it are gone), so a shared work queue discovers and processes related items
as their dependencies permit. The subcommands scope which domains are shown and removed:

  * `all`          — the default; every domain, and one `--prune` removes prunable items
                     as their dependencies permit.
  * `worktrees`    — the worktree slice; `--prune` removes prunable worktrees.
  * `bazel-bases`  — the output-base slice; `--delete` removes prunable bases.
  * `repo-cache`   — the repo-contents-cache slice; `--delete` removes unreferenced entries.

The `all` command also surfaces foreign clones (`foreign_clone_gc`) — other clones of the same
project, found among Bazel output-base workspaces, that are invisible to `git worktree list`
run against this repo. `--prune` alone never removes one (`shutil.rmtree` on an independent git
object store is a bigger blast radius than anything else this tool does); it additionally
needs `--delete-foreign-clones`.

Nothing is removed without an explicit flag. Each removal revalidates its candidate immediately
beforehand and skips one that no longer qualifies: a worktree is re-classified, a branch must
still be at the classified tip, a base or repo-cache entry is rechecked under its Bazel lock.
Skipped items (no longer qualifying, or busy) are reported but exit 0; failed removals exit 1.
Files that git ignores do not count as uncommitted work, so removing a worktree or foreign
clone deletes them. `github_prs` owns GitHub access (PR state); the scan itself is
network-free.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import humanize
import typer
from rich.console import Console
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from tabulate import tabulate

from devinfra.gc import (
    branch_gc,
    foreign_clone_gc,
    git_repo,
    github_prs,
    output_base_gc,
    repo_contents_cache_gc,
    workspace_jobs,
    workspace_queue,
    worktree_gc,
)
from devinfra.gc.branch_gc import BranchClassification, PrunableBranch, RetainedBranch
from devinfra.gc.foreign_clone_gc import ForeignCloneClassification, PrunableForeignClone, RetainedForeignClone
from devinfra.gc.output_base_gc import PrunableBase, default_output_user_root
from devinfra.gc.pull_request import PrInfo
from devinfra.gc.removal import Failed, RemovalResult, Removed, Skipped
from devinfra.gc.repo_contents_cache_gc import PrunableEntry
from devinfra.gc.verdict import verdict_of
from devinfra.gc.work_queue import DEFAULT_WORKERS, WorkQueue
from devinfra.gc.worktree_gc import Classification, PrunableWorktree, RetainedWorktree, ReviewWorktree

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_USER_ROOT = default_output_user_root()
_PROGRESS_BAR_WIDTH = 24


def _short(path: Path) -> str:
    return os.fspath(path).replace(os.fspath(Path.home()), "~")


def _activity(item: Classification) -> str:
    if item.last_activity is None:
        return "?"
    return item.last_activity.astimezone().isoformat(timespec="seconds")


def render_worktrees(items: list[Classification], *, include_kept: bool) -> str:
    visible = items if include_kept else [item for item in items if not isinstance(item, RetainedWorktree)]
    rows = [
        [
            verdict_of(item),
            _activity(item),
            _short(item.worktree.path),
            item.worktree.branch or "(detached)",
            worktree_gc.describe_reason(item),
        ]
        for item in visible
    ]
    counts = {
        "prunable": sum(isinstance(item, PrunableWorktree) for item in items),
        "kept": sum(isinstance(item, RetainedWorktree) for item in items),
        "review": sum(isinstance(item, ReviewWorktree) for item in items),
    }
    headers = ["STATUS", "LAST ACTIVITY", "WORKTREE", "BRANCH", "DETAIL"]
    parts = [tabulate(rows, headers=headers, tablefmt="plain", disable_numparse=True)] if rows else []
    parts.append(
        f"Summary: {len(items)} worktrees; {counts['prunable']} prunable, "
        f"{counts['kept']} kept, {counts['review']} review"
    )
    return "\n".join(parts)


def render_branches(items: list[BranchClassification], *, include_kept: bool) -> str:
    visible = items if include_kept else [item for item in items if not isinstance(item, RetainedBranch)]
    rows = [[verdict_of(item), item.branch.name, branch_gc.describe_reason(item)] for item in visible]
    counts = {
        "prunable": sum(isinstance(item, PrunableBranch) for item in items),
        "kept": sum(isinstance(item, RetainedBranch) for item in items),
        "review": sum(not isinstance(item, PrunableBranch | RetainedBranch) for item in items),
    }
    headers = ["STATUS", "BRANCH", "DETAIL"]
    parts = [tabulate(rows, headers=headers, tablefmt="plain", disable_numparse=True)] if rows else []
    parts.append(
        f"Summary: {len(items)} branches; {counts['prunable']} prunable, "
        f"{counts['kept']} kept, {counts['review']} review"
    )
    return "\n".join(parts)


def render_foreign_clones(items: list[ForeignCloneClassification], *, include_kept: bool) -> str:
    visible = items if include_kept else [item for item in items if not isinstance(item, RetainedForeignClone)]
    rows = [[verdict_of(item), _short(item.clone.root), len(item.clone.members), item.reason] for item in visible]
    counts = {
        "prunable": sum(isinstance(item, PrunableForeignClone) for item in items),
        "kept": sum(isinstance(item, RetainedForeignClone) for item in items),
        "review": sum(isinstance(item, foreign_clone_gc.ReviewForeignClone) for item in items),
    }
    headers = ["STATUS", "ROOT", "WORKTREES", "DETAIL"]
    parts = [tabulate(rows, headers=headers, tablefmt="plain")] if rows else []
    parts.append(
        f"Summary: {len(items)} foreign clones; {counts['prunable']} prunable, "
        f"{counts['kept']} kept, {counts['review']} review"
    )
    return "\n".join(parts)


type _PrSource = Callable[[Path, set[str]], Awaitable[dict[str, PrInfo]]]


async def _without_prs(repo: Path, branches: set[str]) -> dict[str, PrInfo]:
    return {}


def _pr_source(*, no_prs: bool) -> _PrSource:
    """The one place `--no-prs` is decided: where PR state comes from for this run."""
    return _without_prs if no_prs else github_prs.pr_states


class _QueueReporter:
    """Live queue counts; each bar covers only work discovered so far."""

    def __init__(self) -> None:
        self._queue: WorkQueue[workspace_jobs.Event] | None = None
        self._waiting = 0
        self._live: Live | None = None

    def __enter__(self) -> _QueueReporter:
        self._live = Live(self.render(), console=Console(stderr=True), transient=True)
        self._live.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._live is not None
        self._live.stop()
        self._live = None

    def update(self, queue: WorkQueue[workspace_jobs.Event], *, waiting: int) -> None:
        self._queue, self._waiting = queue, waiting
        if self._live is not None:
            self._live.update(self.render())

    def render(self) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column()
        for _ in range(3):
            table.add_column(justify="right")
        table.add_column()
        table.add_column()
        table.add_column(justify="right")
        table.add_row("work", "ready", "active", "finished", "outcomes", "discovered", "")
        if self._queue is None:
            return table
        phases = self._queue.phases
        table.add_row(
            "queue",
            str(sum(p.ready for p in phases.values())),
            str(sum(p.running for p in phases.values())),
            str(sum(p.finished for p in phases.values())),
            f"{self._waiting} waiting",
            "",
            "",
        )
        for name, phase in phases.items():
            total = phase.ready + phase.running + phase.finished
            outcomes = ", ".join(f"{count} {name}" for name, count in sorted(phase.outcomes.items()))
            table.add_row(
                name,
                str(phase.ready),
                str(phase.running),
                str(phase.finished),
                outcomes,
                ProgressBar(total=total, completed=phase.finished, width=_PROGRESS_BAR_WIDTH),
                f"{phase.finished}/{total}",
            )
        return table


@dataclass(frozen=True, slots=True)
class _RemovalLabels[S]:
    """How one domain's removal results read in the report."""

    noun: str  # "worktree": "REMOVED worktree <subject>"
    plural: str  # "worktrees": the collapsed "SKIPPED 3 worktrees: <reason>"
    summary: str  # "Worktrees": heads the summary line
    verb: str  # "removed" or "deleted"
    subject: Callable[[S], str]
    leftover: str | None = None  # names `Failed.leftover` in the line, e.g. "quarantine"


@dataclass(frozen=True, slots=True)
class _RemovalOutcome[S]:
    removed: frozenset[S]
    ok: bool


_WORKTREE_LABELS = _RemovalLabels("worktree", "worktrees", "Worktrees", "removed", _short)
_BRANCH_LABELS = _RemovalLabels("branch", "branches", "Branches", "deleted", str)
_BASE_LABELS = _RemovalLabels("base", "bases", "Bases", "deleted", _short, leftover="quarantine")
_FOREIGN_CLONE_LABELS = _RemovalLabels("foreign clone", "foreign clones", "Foreign clones", "removed", _short)


def _report_removals[S](
    results: Sequence[RemovalResult[S]], labels: _RemovalLabels[S], *, itemize: bool = True, summary_suffix: str = ""
) -> _RemovalOutcome[S]:
    """Print one removal step's results and its summary line.

    Failures always print. With `itemize` off, removals are silent and skips collapse to one
    line per distinct reason.
    """
    removed: set[S] = set()
    skip_reasons: Counter[str] = Counter()
    failed = 0
    for result in results:
        name = labels.subject(result.subject)
        match result:
            case Removed():
                removed.add(result.subject)
                if itemize:
                    print(f"{labels.verb.upper()} {labels.noun} {name}")
            case Skipped():
                skip_reasons[result.reason] += 1
                if itemize:
                    print(f"SKIPPED {labels.noun} {name}: {result.reason}", file=sys.stderr)
            case Failed():
                failed += 1
                leftover = (
                    f"; {labels.leftover}={result.leftover}"
                    if labels.leftover is not None and result.leftover is not None
                    else ""
                )
                print(f"FAILED {labels.noun} {name}: {result.error}{leftover}", file=sys.stderr)
    if not itemize:
        for reason, count in skip_reasons.items():
            print(f"SKIPPED {count} {labels.plural}: {reason}", file=sys.stderr)
    skipped = skip_reasons.total()
    print(f"{labels.summary}: {len(removed)} {labels.verb}, {skipped} skipped, {failed} failed{summary_suffix}")
    return _RemovalOutcome(frozenset(removed), ok=failed == 0)


@dataclass(frozen=True, slots=True)
class _Command:
    """The domains one subcommand shows, and removes when it is told to."""

    domains: frozenset[workspace_queue.Domain]
    dry_run_hint: str


_WORKTREES_COMMAND = _Command(
    frozenset({workspace_queue.Domain.WORKTREES}), "Dry run only; pass --prune to remove the prunable worktrees."
)
_BASES_COMMAND = _Command(
    frozenset({workspace_queue.Domain.BASES}), "Dry run only; pass --delete to remove the prunable bases."
)
_REPO_CACHE_COMMAND = _Command(
    frozenset({workspace_queue.Domain.REPO_CACHE}), "Dry run only; pass --delete to remove the unreferenced entries."
)
_ALL_COMMAND = _Command(
    frozenset(workspace_queue.Domain),
    "Dry run only; pass --prune to remove all prunable worktrees, branches, bases, and repo-cache entries.",
)


@dataclass(frozen=True, slots=True)
class _Options:
    output_user_root: Path
    show_all: bool
    sizes: bool
    verbose: bool
    workers: int
    apply: bool  # --prune / --delete
    delete_foreign_clones: bool


def _print_findings(command: _Command, options: _Options, findings: workspace_queue.Report) -> None:
    scan, entries = findings.scan, findings.entries
    contents = repo_contents_cache_gc.repo_contents_cache(options.output_user_root)
    sections: list[tuple[workspace_queue.Domain, str, Callable[[], str]]] = [
        (
            workspace_queue.Domain.WORKTREES,
            "Worktrees",
            lambda: render_worktrees(scan.worktrees, include_kept=options.show_all),
        ),
        (
            workspace_queue.Domain.BRANCHES,
            "Branches",
            lambda: render_branches(scan.branches, include_kept=options.show_all),
        ),
        (
            workspace_queue.Domain.BASES,
            "Bazel output bases",
            lambda: output_base_gc.render_report(
                scan.bases,
                include_kept=options.show_all,
                include_sizes=options.sizes,
                measured_sizes=findings.base_sizes,
            ),
        ),
        (
            workspace_queue.Domain.REPO_CACHE,
            "Repo contents cache",
            lambda: repo_contents_cache_gc.render_report(
                entries,
                contents=contents,
                include_kept=options.show_all,
                include_sizes=options.sizes,
                include_prunable=options.verbose,
                measured_sizes=findings.cache_sizes,
            ),
        ),
        (
            workspace_queue.Domain.FOREIGN_CLONES,
            "Foreign clones",
            lambda: render_foreign_clones(scan.foreign_clones, include_kept=options.show_all),
        ),
    ]
    shown = [(title, render) for domain, title, render in sections if domain in command.domains]
    for index, (title, render) in enumerate(shown):
        if len(shown) > 1:
            print(f"{'\n' if index else ''}# {title}")
        print(render())


def _any_prunable(command: _Command, findings: workspace_queue.Report) -> bool:
    scan, domains = findings.scan, command.domains
    return (
        (workspace_queue.Domain.WORKTREES in domains and any(isinstance(i, PrunableWorktree) for i in scan.worktrees))
        or (workspace_queue.Domain.BRANCHES in domains and any(isinstance(i, PrunableBranch) for i in scan.branches))
        or (workspace_queue.Domain.BASES in domains and any(isinstance(i, PrunableBase) for i in scan.bases))
        or (
            workspace_queue.Domain.REPO_CACHE in domains and any(isinstance(i, PrunableEntry) for i in findings.entries)
        )
    )


def _report_queue_results(options: _Options, report: workspace_queue.Report) -> bool:
    outcomes: list[bool] = []
    if report.worktree_removals:
        outcomes.append(_report_removals(report.worktree_removals, _WORKTREE_LABELS).ok)
    if report.branch_removals:
        outcomes.append(_report_removals(report.branch_removals, _BRANCH_LABELS).ok)
    if report.base_removals:
        assert report.base_free_before is not None
        change = shutil.disk_usage(options.output_user_root).free - report.base_free_before
        outcomes.append(
            _report_removals(
                report.base_removals,
                _BASE_LABELS,
                summary_suffix=f"; free-space change {humanize.naturalsize(change, binary=True)}",
            ).ok
        )
    if report.cache_removals:
        contents = repo_contents_cache_gc.repo_contents_cache(options.output_user_root)
        labels: _RemovalLabels[Path] = _RemovalLabels(
            "repo-cache entry",
            "repo-cache entries",
            "Repo cache",
            "deleted",
            lambda path: os.path.relpath(path, contents),
            leftover="trash",
        )
        suffix = ""
        if report.cache_free_before is not None:
            change = shutil.disk_usage(contents).free - report.cache_free_before
            suffix = f"; free-space change {humanize.naturalsize(change, binary=True)}"
        outcomes.append(
            _report_removals(report.cache_removals, labels, itemize=options.verbose, summary_suffix=suffix).ok
        )
    if report.foreign_removals:
        outcomes.append(_report_removals(report.foreign_removals, _FOREIGN_CLONE_LABELS).ok)
    return all(outcomes)


async def run(command: _Command, options: _Options, *, repo: Path | None, pr_source: _PrSource) -> None:
    domains = command.domains
    session_bases = (
        tuple(output_base_gc.session_env_output_bases())
        if domains
        & {workspace_queue.Domain.BASES, workspace_queue.Domain.REPO_CACHE, workspace_queue.Domain.FOREIGN_CLONES}
        else ()
    )
    try:
        queue_options = workspace_queue.Options(
            domains,
            options.output_user_root,
            session_bases,
            options.workers,
            options.apply,
            options.sizes,
            options.delete_foreign_clones,
            repo,
            pr_source,
        )
        with _QueueReporter() as reporter:
            report = await workspace_queue.run(queue_options, reporter)
    except workspace_queue.QueueError as error:
        _report_queue_results(options, error.report)
        print(error.error, file=sys.stderr)
        raise SystemExit(1) from error
    except workspace_queue.QueueInterrupted as error:
        _report_queue_results(options, error.report)
        print("Interrupted; queued work was cancelled.", file=sys.stderr)
        raise SystemExit(130) from error
    except (*git_repo.INSPECTION_ERRORS, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error

    if options.apply and any(
        (
            report.worktree_removals,
            report.branch_removals,
            report.base_removals,
            report.cache_removals,
            report.foreign_removals,
        )
    ):
        print("Findings reflect each item when scanned; removals may already have completed.")
    _print_findings(command, options, report)
    multiple = len(domains) > 1
    foreign_prunable = workspace_queue.Domain.FOREIGN_CLONES in domains and any(
        isinstance(i, PrunableForeignClone) for i in report.scan.foreign_clones
    )
    if not options.apply:
        if _any_prunable(command, report):
            print(f"{'\n' if multiple else ''}{command.dry_run_hint}")
        if foreign_prunable:
            print("Foreign clones are prunable too; pass --prune --delete-foreign-clones to remove them.")
        return
    if multiple:
        print()
    ok = _report_queue_results(options, report)
    if foreign_prunable and not options.delete_foreign_clones:
        print("Foreign clones were left alone; pass --delete-foreign-clones to remove them too.")
    if not ok:
        raise SystemExit(1)


app = typer.Typer(help=__doc__, add_completion=False)

_RepoOption = Annotated[Path, typer.Option("--repo", help="a path inside the repo")]
_RootOption = Annotated[Path, typer.Option("--output-user-root", help="Bazel output user root")]
_AllOption = Annotated[bool, typer.Option("--all", help="show all items, including kept repo-cache entries")]
_NoPrsOption = Annotated[bool, typer.Option("--no-prs", help="skip the GitHub PR cross-check (git signals only)")]
_SizesOption = Annotated[bool, typer.Option("--sizes", help="calculate base sizes with du (potentially slow)")]
_VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="show detailed scan and repo-cache output")]
_WorkersOption = Annotated[
    int, typer.Option("--workers", min=1, help="maximum concurrent discovery, inspection, and removal jobs")
]


def _configure_logging(*, verbose: bool) -> None:
    # The compact live queue indicator is the default on-TTY signal for
    # a scan in progress; --verbose additionally streams the per-item `logger.info` trace this
    # module already emits, which would otherwise bury that indicator in scroll.
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(message)s")


@app.command("all")
def _all_command(
    repo: _RepoOption = Path(),
    output_user_root: _RootOption = _DEFAULT_OUTPUT_USER_ROOT,
    show_all: _AllOption = False,
    no_prs: _NoPrsOption = False,
    sizes: _SizesOption = False,
    verbose: _VerboseOption = False,
    workers: _WorkersOption = DEFAULT_WORKERS,
    prune: Annotated[
        bool,
        typer.Option("--prune", help="remove all prunable worktrees, branches, output bases, and repo-cache entries"),
    ] = False,
    delete_foreign_clones: Annotated[
        bool,
        typer.Option(
            "--delete-foreign-clones",
            help="with --prune, also rmtree prunable foreign clones (bigger blast radius: an independent git object store, not a linked worktree)",
        ),
    ] = False,
) -> None:
    """Classify worktrees, branches, and output bases together (the default command)."""
    _configure_logging(verbose=verbose)
    options = _Options(
        output_user_root=output_user_root,
        show_all=show_all,
        sizes=sizes,
        verbose=verbose,
        workers=workers,
        apply=prune,
        delete_foreign_clones=delete_foreign_clones,
    )
    asyncio.run(run(_ALL_COMMAND, options, repo=repo, pr_source=_pr_source(no_prs=no_prs)))


@app.command("worktrees")
def _worktrees_command(
    repo: _RepoOption = Path(),
    show_all: _AllOption = False,
    no_prs: _NoPrsOption = False,
    verbose: _VerboseOption = False,
    workers: _WorkersOption = DEFAULT_WORKERS,
    prune: Annotated[bool, typer.Option("--prune", help="remove prunable worktrees (revalidated first)")] = False,
) -> None:
    """The worktree slice of the joint scan."""
    _configure_logging(verbose=verbose)
    options = _Options(
        output_user_root=_DEFAULT_OUTPUT_USER_ROOT,
        show_all=show_all,
        sizes=False,
        verbose=False,
        workers=workers,
        apply=prune,
        delete_foreign_clones=False,
    )
    asyncio.run(run(_WORKTREES_COMMAND, options, repo=repo, pr_source=_pr_source(no_prs=no_prs)))


@app.command("bazel-bases")
def _bases_command(
    repo: _RepoOption = Path(),
    output_user_root: _RootOption = _DEFAULT_OUTPUT_USER_ROOT,
    show_all: _AllOption = False,
    no_prs: _NoPrsOption = False,
    sizes: _SizesOption = False,
    verbose: _VerboseOption = False,
    workers: _WorkersOption = DEFAULT_WORKERS,
    delete: Annotated[bool, typer.Option("--delete", help="revalidate and remove prunable output bases")] = False,
) -> None:
    """The Bazel output-base slice of the joint scan."""
    _configure_logging(verbose=verbose)
    options = _Options(
        output_user_root=output_user_root,
        show_all=show_all,
        sizes=sizes,
        verbose=False,
        workers=workers,
        apply=delete,
        delete_foreign_clones=False,
    )
    asyncio.run(run(_BASES_COMMAND, options, repo=repo, pr_source=_pr_source(no_prs=no_prs)))


@app.command("repo-cache")
def _repo_cache_command(
    output_user_root: _RootOption = _DEFAULT_OUTPUT_USER_ROOT,
    show_all: _AllOption = False,
    sizes: Annotated[
        bool, typer.Option("--sizes", help="calculate entry sizes with du (slow: thousands of entries)")
    ] = False,
    verbose: _VerboseOption = False,
    workers: _WorkersOption = DEFAULT_WORKERS,
    delete: Annotated[
        bool, typer.Option("--delete", help="revalidate under Bazel's cache lock and remove unreferenced entries")
    ] = False,
) -> None:
    """The repo-contents-cache slice of the joint scan: entries no output base links to."""
    _configure_logging(verbose=verbose)
    options = _Options(
        output_user_root=output_user_root,
        show_all=show_all,
        sizes=sizes,
        verbose=verbose,
        workers=workers,
        apply=delete,
        delete_foreign_clones=False,
    )
    asyncio.run(run(_REPO_CACHE_COMMAND, options, repo=None, pr_source=_without_prs))


_COMMANDS = {"all", "worktrees", "bazel-bases", "repo-cache"}


def _with_default_command(argv: list[str]) -> list[str]:
    """Route a bare invocation (or one that starts with an option) to the `all` command, so
    `workspace-gc` and `workspace-gc --no-prs` behave as `workspace-gc all ...`."""
    if not argv:
        return ["all"]
    if argv[0] in _COMMANDS or argv[0] in ("-h", "--help"):
        return argv
    return ["all", *argv]


def main(argv: list[str] | None = None) -> None:
    """Run the CLI through its importable wheel entry point."""
    try:
        app(args=_with_default_command(sys.argv[1:] if argv is None else argv))
    except git_repo.INSPECTION_ERRORS as error:
        print(f"workspace-gc: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

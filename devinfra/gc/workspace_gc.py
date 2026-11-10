"""`workspace-gc`: reclaim local development state — worktrees, branches, Bazel output bases,
and Bazel's repo contents cache.

The domains are coupled (a branch checked out in a live worktree can't be deleted; an output
base orphans when its workspace worktree is removed, and a base for a workspace under `/tmp`
can be reclaimed without removing its workspace; a repo-cache entry is unreferenced once the
bases linking to it are gone), so one joint scan (`workspace_scan.scan_workspace`, then
`repo_contents_cache_gc.scan_repo_contents_cache` over its bases) classifies them together
and the subcommands are views of that single result:

  * `all`          — the default; every domain, and one `--prune` removes all prunable
                     items in dependency order (worktrees → branches → bases → repo cache).
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
import enum
import logging
import os
import shutil
import signal
import sys
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Annotated

import humanize
import typer
from rich.console import Console
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text
from tabulate import tabulate

from devinfra.gc import (
    branch_gc,
    foreign_clone_gc,
    git_repo,
    github_prs,
    output_base_gc,
    repo_contents_cache_gc,
    workspace_scan,
    worktree_gc,
)
from devinfra.gc.branch_gc import BranchClassification, PrunableBranch, RetainedBranch
from devinfra.gc.foreign_clone_gc import ForeignCloneClassification, PrunableForeignClone, RetainedForeignClone
from devinfra.gc.git_repo import GitError
from devinfra.gc.output_base_gc import PrunableBase, default_output_user_root
from devinfra.gc.pull_request import PrInfo
from devinfra.gc.removal import Failed, RemovalResult, Removed, Skipped
from devinfra.gc.repo_contents_cache_gc import EntryInspection, PrunableEntry
from devinfra.gc.scan_progress import NULL_PROGRESS, ProgressSink
from devinfra.gc.verdict import verdict_of
from devinfra.gc.workspace_scan import WorkspaceScan
from devinfra.gc.worktree_gc import Classification, PrunableWorktree, RetainedWorktree, ReviewWorktree

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_USER_ROOT = default_output_user_root()
_SCAN_COLUMNS = {"PRUNE": "prune", "KEEP": "keep", "REVIEW": "review"}
_REMOVAL_COLUMNS = {"REMOVED": "removed", "SKIPPED": "skipped", "FAILED": "failed"}
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


def _active_worktree(repo: Path) -> Path | None:
    toplevel = git_repo.git(repo, "rev-parse", "--show-toplevel", check=False).stdout.strip()
    return Path(toplevel) if toplevel else None


type _PrSource = Callable[[Path, set[str]], Awaitable[dict[str, PrInfo]]]


async def _without_prs(repo: Path, branches: set[str]) -> dict[str, PrInfo]:
    return {}


def _pr_source(*, no_prs: bool) -> _PrSource:
    """The one place `--no-prs` is decided: where PR state comes from for this run."""
    return _without_prs if no_prs else github_prs.pr_states


@dataclass(frozen=True, slots=True)
class _RunContext:
    repo: Path
    workers: int
    prs: dict[str, PrInfo]


@dataclass(slots=True)
class _PhaseProgress:
    total: int
    counts: dict[str, int]


class _ProgressReporter:
    """Live per-phase progress rendered as a borderless table: one row per phase, with its
    outcome counts in aligned columns and a bar; a no-op off a TTY (`rich.live.Live` detects
    this itself).

    A scan over hundreds of worktrees and branches, or the removal of large trees, can take
    minutes with nothing else to show for it in the meantime — this renders in place instead
    of the wall of `logger.info` lines underneath (still there, for `--verbose`). Updates take
    a lock, which the classification worker pools rely on. `transient=True` clears the table
    on completion — the CLI's own report print, right after, is the record of the outcome.

    Used as a context manager around each scan or removal step: `Live` owns the terminal's
    bottom region while live, so any other output must happen with it stopped, not
    interleaved. Each entry starts with an empty table.
    """

    def __init__(self, columns: Mapping[str, str]) -> None:
        self._columns = columns  # outcome key → column header
        self._console = Console(stderr=True)
        self._phases: dict[str, _PhaseProgress] = {}
        self._live: Live | None = None
        self._lock = threading.Lock()

    @classmethod
    def for_scans(cls) -> _ProgressReporter:
        return cls(_SCAN_COLUMNS)

    @classmethod
    def for_removals(cls) -> _ProgressReporter:
        return cls(_REMOVAL_COLUMNS)

    def __enter__(self) -> _ProgressReporter:
        self._phases.clear()
        self._live = Live(self.render(), console=self._console, transient=True)
        self._live.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._live is not None
        self._live.stop()
        self._live = None

    def render(self) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column()
        for _ in self._columns:
            table.add_column(justify="right")
        table.add_column()
        table.add_column(justify="right")
        table.add_row("", *(Text(label, style="dim") for label in self._columns.values()), "", "")
        for phase, state in self._phases.items():
            done = sum(state.counts.values())
            table.add_row(
                phase,
                *(str(state.counts[key]) for key in self._columns),
                ProgressBar(total=state.total, completed=done, width=_PROGRESS_BAR_WIDTH),
                f"{done}/{state.total}",
            )
        return table

    def _refresh(self) -> None:
        assert self._live is not None
        self._live.update(self.render())

    def start_phase(self, phase: str, total: int) -> None:
        with self._lock:
            self._phases[phase] = _PhaseProgress(total, dict.fromkeys(self._columns, 0))
            self._refresh()

    def record(self, phase: str, outcome: str) -> None:
        with self._lock:
            self._phases[phase].counts[outcome] += 1
            self._refresh()


def _scan(
    context: _RunContext,
    *,
    output_user_root: Path | None,
    session_bases: Sequence[Path],
    progress: ProgressSink,
    foreign_clone_roots: Sequence[Path] = (),
) -> WorkspaceScan:
    pg = git_repo.open_repo(context.repo)
    return workspace_scan.scan_workspace(
        context.repo,
        main=git_repo.main_ref(pg),
        default_branch=git_repo.default_branch_name(pg),
        pr_states=context.prs,
        session_bases=session_bases,
        active_path=_active_worktree(context.repo),
        output_user_root=output_user_root,
        foreign_clone_roots=foreign_clone_roots,
        workers=context.workers,
        progress=progress,
    )


async def _run_bounded[T, R](
    items: Sequence[T], function: Callable[[T], R], *, workers: int, on_done: Callable[[R], None] | None = None
) -> list[R]:
    """Run blocking `function` over `items` on worker threads, `workers` at a time; results are in
    `items` order. `function` must be independent per item. `on_done` sees each result as it
    finishes, on the event-loop thread."""
    semaphore = asyncio.Semaphore(workers)

    async def run_one(item: T) -> R:
        async with semaphore:
            result = await asyncio.to_thread(function, item)
        if on_done is not None:
            on_done(result)
        return result

    return list(await asyncio.gather(*(run_one(item) for item in items)))


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


def _removal_outcome[S](result: RemovalResult[S]) -> str:
    match result:
        case Removed():
            return "REMOVED"
        case Skipped():
            return "SKIPPED"
        case Failed():
            return "FAILED"


async def _remove_all[T, S](
    items: Sequence[T], function: Callable[[T], RemovalResult[S]], labels: _RemovalLabels[S], *, workers: int
) -> list[RemovalResult[S]]:
    """Run the removals with a live removed/skipped/failed row; the caller reports once it clears."""
    with _ProgressReporter.for_removals() as progress:
        progress.start_phase(labels.plural, len(items))
        return await _run_bounded(
            items,
            function,
            workers=workers,
            on_done=lambda result: progress.record(labels.plural, _removal_outcome(result)),
        )


def _nothing_to_remove[S]() -> _RemovalOutcome[S]:
    return _RemovalOutcome(frozenset(), ok=True)


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


async def _apply_worktree_removals(context: _RunContext, worktrees: list[Classification]) -> _RemovalOutcome[Path]:
    """Remove every prunable worktree that is still prunable now."""
    candidates = [item for item in worktrees if isinstance(item, PrunableWorktree)]
    if not candidates:
        return _nothing_to_remove()
    logger.info("Removing %d worktrees", len(candidates))
    main_path = git_repo.main_worktree(context.repo)
    active_path = _active_worktree(context.repo)
    live = worktree_gc.processes_by_worktree(item.worktree.path for item in candidates)

    def remove_one(item: PrunableWorktree) -> RemovalResult[Path]:
        logger.info("Removing worktree %s", item.worktree.path)
        return worktree_gc.remove_if_still_prunable(
            context.repo,
            item.worktree,
            main=item.main,
            pr_states=context.prs,
            main_path=main_path,
            active_path=active_path,
            live_pids=live.get(item.worktree.path, []),
        )

    return _report_removals(
        await _remove_all(candidates, remove_one, _WORKTREE_LABELS, workers=context.workers), _WORKTREE_LABELS
    )


def _apply_branch_deletions(repo: Path, branches: list[BranchClassification]) -> _RemovalOutcome[str]:
    """Delete every prunable branch that has not moved since it was classified."""
    candidates = [item for item in branches if isinstance(item, PrunableBranch)]
    if not candidates:
        return _nothing_to_remove()
    logger.info("Deleting %d branches", len(candidates))
    results = []
    for index, item in enumerate(candidates, start=1):
        logger.info("Deleting branch %d/%d %s", index, len(candidates), item.branch.name)
        results.append(branch_gc.delete_branch(repo, item.branch.name, expected_tip=item.tip))
    return _report_removals(results, _BRANCH_LABELS)


async def _apply_base_deletions(
    candidates: Sequence[PrunableBase], *, output_user_root: Path, workers: int
) -> _RemovalOutcome[Path]:
    """Delete the given prunable output bases; `delete_prunable_bases` revalidates each one under
    its Bazel lock, so the caller's scan needs no refresh beyond what it already knows."""
    if not candidates:
        return _nothing_to_remove()
    free_before = shutil.disk_usage(output_user_root).free
    results = await _remove_all(candidates, output_base_gc.delete_prunable_base, _BASE_LABELS, workers=workers)
    free_change = shutil.disk_usage(output_user_root).free - free_before
    return _report_removals(
        results, _BASE_LABELS, summary_suffix=f"; free-space change {humanize.naturalsize(free_change, binary=True)}"
    )


async def _apply_repo_cache_deletions(
    candidates: Sequence[PrunableEntry],
    *,
    output_user_root: Path,
    session_bases: Sequence[Path],
    verbose: bool,
    workers: int,
) -> _RemovalOutcome[Path]:
    """Delete the given unreferenced repo-cache entries.

    `delete_prunable_entries` revalidates each entry under Bazel's cache lock against a fresh
    link pass, so the caller's classification needs no refresh.
    """
    if not candidates:
        return _nothing_to_remove()
    contents = repo_contents_cache_gc.repo_contents_cache(output_user_root)
    free_before = shutil.disk_usage(contents).free
    trashed = await asyncio.to_thread(
        repo_contents_cache_gc.trash_prunable_entries,
        candidates,
        contents=contents,
        output_user_root=output_user_root,
        session_bases=session_bases,
    )
    labels: _RemovalLabels[Path] = _RemovalLabels(
        "repo-cache entry",
        "repo-cache entries",
        "Repo cache",
        "deleted",
        lambda path: os.path.relpath(path, contents),
        leftover="trash",
    )
    results = [
        *trashed.results,
        *await _remove_all(
            trashed.trashed, lambda item: repo_contents_cache_gc.empty_trash(*item), labels, workers=workers
        ),
    ]
    free_change = shutil.disk_usage(contents).free - free_before
    return _report_removals(
        results,
        labels,
        itemize=verbose,
        summary_suffix=f"; free-space change {humanize.naturalsize(free_change, binary=True)}",
    )


async def _apply_foreign_clone_deletions(
    context: _RunContext, output_user_root: Path, session_bases: Sequence[Path]
) -> _RemovalOutcome[Path]:
    """Remove every prunable foreign clone.

    Fresh discovery + classification right here is the revalidation: nothing computed before
    this call is reused, so a clone that turned dirty (or gained an open PR) in the meantime is
    never passed to `prune_foreign_clone`.
    """
    roots = workspace_scan.foreign_clone_roots(context.repo, output_user_root, session_bases=session_bases)
    active_path = _active_worktree(context.repo)

    def classify_one(root: Path) -> foreign_clone_gc.ForeignCloneClassification:
        return foreign_clone_gc.classify_foreign_clone(root, pr_states=context.prs, active_path=active_path)

    classifications = workspace_scan.parallel_classify(
        roots,
        lambda: classify_one,
        phase="foreign_clones",
        noun="foreign clone",
        progress=NULL_PROGRESS,
        category_of=verdict_of,
        describe=str,
        workers=context.workers,
    )
    candidates = [item.clone for item in classifications if isinstance(item, PrunableForeignClone)]
    if not candidates:
        return _nothing_to_remove()
    logger.info("Removing %d foreign clones", len(candidates))
    return _report_removals(
        await _remove_all(
            candidates, foreign_clone_gc.prune_foreign_clone, _FOREIGN_CLONE_LABELS, workers=context.workers
        ),
        _FOREIGN_CLONE_LABELS,
    )


class _Domain(enum.StrEnum):
    WORKTREES = enum.auto()
    BRANCHES = enum.auto()
    BASES = enum.auto()
    REPO_CACHE = enum.auto()
    FOREIGN_CLONES = enum.auto()


@dataclass(frozen=True, slots=True)
class _Command:
    """The domains one subcommand shows, and removes when it is told to."""

    domains: frozenset[_Domain]
    dry_run_hint: str


_WORKTREES_COMMAND = _Command(
    frozenset({_Domain.WORKTREES}), "Dry run only; pass --prune to remove the prunable worktrees."
)
_BASES_COMMAND = _Command(frozenset({_Domain.BASES}), "Dry run only; pass --delete to remove the prunable bases.")
_REPO_CACHE_COMMAND = _Command(
    frozenset({_Domain.REPO_CACHE}), "Dry run only; pass --delete to remove the unreferenced entries."
)
_ALL_COMMAND = _Command(
    frozenset(_Domain),
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


@dataclass(frozen=True, slots=True)
class _Findings:
    scan: WorkspaceScan
    entries: list[EntryInspection]
    context: _RunContext | None  # None for the repo-cache view, which never touches git or PRs


async def _collect(
    command: _Command,
    options: _Options,
    *,
    repo: Path | None,
    pr_source: _PrSource,
    session_bases: Sequence[Path],
    progress: _ProgressReporter,
) -> _Findings:
    """Scan what `command` shows. A view of worktrees, branches or foreign clones runs the joint
    scan. The bases views scan the filesystem alone: the only git input a base needs is whether
    its workspace is a prunable worktree, so their PR query covers just those workspaces' branches
    and no branch is classified."""
    domains = command.domains
    shows_bases = bool(domains & {_Domain.BASES, _Domain.REPO_CACHE})
    contents = repo_contents_cache_gc.repo_contents_cache(options.output_user_root)

    async def resolve_context(branches: set[str]) -> _RunContext:
        assert repo is not None
        return _RunContext(repo=repo, workers=options.workers, prs=await pr_source(repo, branches))

    context: _RunContext | None = None
    if domains & {_Domain.WORKTREES, _Domain.BRANCHES, _Domain.FOREIGN_CLONES}:
        assert repo is not None
        foreign_roots = (
            workspace_scan.foreign_clone_roots(repo, options.output_user_root, session_bases=session_bases)
            if _Domain.FOREIGN_CLONES in domains
            else []
        )
        context = await resolve_context(
            workspace_scan.pr_branch_candidates(repo) | workspace_scan.foreign_clone_branch_candidates(foreign_roots)
        )
        with progress:
            scan = _scan(
                context,
                output_user_root=options.output_user_root if shows_bases else None,
                session_bases=session_bases if shows_bases else (),
                progress=progress,
                foreign_clone_roots=foreign_roots,
            )
            entries = (
                repo_contents_cache_gc.scan_repo_contents_cache(contents, scan.bases, progress=progress)
                if _Domain.REPO_CACHE in domains
                else []
            )
        return _Findings(scan, entries, context)

    with progress:
        bases = output_base_gc.scan_output_user_root(
            options.output_user_root, session_bases=session_bases, progress=progress
        )
        entries = (
            repo_contents_cache_gc.scan_repo_contents_cache(contents, bases, progress=progress)
            if _Domain.REPO_CACHE in domains
            else []
        )
    if repo is not None:
        context = await resolve_context(workspace_scan.base_workspace_branches(repo, bases))
        with progress:
            bases = workspace_scan.annotate_bases(
                repo,
                bases,
                main=git_repo.main_ref(git_repo.open_repo(repo)),
                pr_states=context.prs,
                active_path=_active_worktree(repo),
                workers=options.workers,
                progress=progress,
            )
    return _Findings(WorkspaceScan(worktrees=[], branches=[], bases=bases), entries, context)


def _print_findings(command: _Command, options: _Options, findings: _Findings) -> None:
    scan, entries = findings.scan, findings.entries
    contents = repo_contents_cache_gc.repo_contents_cache(options.output_user_root)
    sections: list[tuple[_Domain, str, Callable[[], str]]] = [
        (_Domain.WORKTREES, "Worktrees", lambda: render_worktrees(scan.worktrees, include_kept=options.show_all)),
        (_Domain.BRANCHES, "Branches", lambda: render_branches(scan.branches, include_kept=options.show_all)),
        (
            _Domain.BASES,
            "Bazel output bases",
            lambda: output_base_gc.render_report(
                scan.bases, include_kept=options.show_all, include_sizes=options.sizes
            ),
        ),
        (
            _Domain.REPO_CACHE,
            "Repo contents cache",
            lambda: repo_contents_cache_gc.render_report(
                entries,
                contents=contents,
                include_kept=options.show_all,
                include_sizes=options.sizes,
                include_prunable=options.verbose,
            ),
        ),
        (
            _Domain.FOREIGN_CLONES,
            "Foreign clones",
            lambda: render_foreign_clones(scan.foreign_clones, include_kept=options.show_all),
        ),
    ]
    shown = [(title, render) for domain, title, render in sections if domain in command.domains]
    for index, (title, render) in enumerate(shown):
        if len(shown) > 1:
            print(f"{'\n' if index else ''}# {title}")
        print(render())


def _any_prunable(command: _Command, findings: _Findings) -> bool:
    """Whether a dry run should point at the apply flag (foreign clones have a flag of their own)."""
    scan, domains = findings.scan, command.domains
    return (
        (_Domain.WORKTREES in domains and any(isinstance(i, PrunableWorktree) for i in scan.worktrees))
        or (_Domain.BRANCHES in domains and any(isinstance(i, PrunableBranch) for i in scan.branches))
        or (_Domain.BASES in domains and any(isinstance(i, PrunableBase) for i in scan.bases))
        or (_Domain.REPO_CACHE in domains and any(isinstance(i, PrunableEntry) for i in findings.entries))
    )


async def _apply(
    command: _Command,
    options: _Options,
    findings: _Findings,
    *,
    session_bases: Sequence[Path],
    progress: _ProgressReporter,
) -> bool:
    """Remove what `command` shows, in dependency order; returns whether every step succeeded.

    Worktrees go first so branches they hold are freed and their bases orphan, and bases before the
    repo cache so entries only they referenced become unreferenced. Worktrees reuse the scan
    (`git worktree remove` itself refuses a tree that turned dirty); branches are re-classified
    because the removals change who holds them. Every step runs regardless of an earlier one's
    failures — each domain is independent.
    """
    domains, scan, context = command.domains, findings.scan, findings.context
    oks: list[bool] = []
    if _Domain.WORKTREES in domains:
        assert context is not None
        oks.append((await _apply_worktree_removals(context, scan.worktrees)).ok)
    if _Domain.BRANCHES in domains:
        assert context is not None
        pg = git_repo.open_repo(context.repo)
        with progress:
            branches = workspace_scan.scan_branches(
                context.repo,
                worktrees=scan.worktrees,
                main=git_repo.main_ref(pg),
                default_branch=git_repo.default_branch_name(pg),
                pr_states=context.prs,
                workers=context.workers,
                progress=progress,
            )
        oks.append(_apply_branch_deletions(context.repo, branches).ok)
    bases_removed: frozenset[Path] = frozenset()
    if _Domain.BASES in domains:
        # Removing worktrees orphans bases the first scan kept, so only then are bases rescanned.
        bases = (
            output_base_gc.scan_output_user_root(options.output_user_root, session_bases=session_bases)
            if _Domain.WORKTREES in domains
            else scan.bases
        )
        outcome = await _apply_base_deletions(
            [item for item in bases if isinstance(item, PrunableBase)],
            output_user_root=options.output_user_root,
            workers=options.workers,
        )
        oks.append(outcome.ok)
        bases_removed = outcome.removed
    if _Domain.REPO_CACHE in domains:
        # An entry is newly unreferenced exactly when every base linking to it was just deleted.
        cache_outcome = await _apply_repo_cache_deletions(
            repo_contents_cache_gc.unreferenced_after_deleting(findings.entries, bases_removed),
            output_user_root=options.output_user_root,
            session_bases=session_bases,
            verbose=options.verbose,
            workers=options.workers,
        )
        oks.append(cache_outcome.ok)
    if _Domain.FOREIGN_CLONES in domains:
        assert context is not None
        if options.delete_foreign_clones:
            oks.append((await _apply_foreign_clone_deletions(context, options.output_user_root, session_bases)).ok)
        elif any(isinstance(i, PrunableForeignClone) for i in scan.foreign_clones):
            print("Foreign clones were left alone; pass --delete-foreign-clones to remove them too.")
    return all(oks)


async def run(command: _Command, options: _Options, *, repo: Path | None, pr_source: _PrSource) -> None:
    """Scan, report and (with `options.apply`) remove the domains of one subcommand.

    `repo` is None only for the repo-cache view, which needs no git.
    """
    progress = _ProgressReporter.for_scans()
    domains = command.domains
    session_bases = (
        output_base_gc.session_env_output_bases()
        if domains & {_Domain.BASES, _Domain.REPO_CACHE, _Domain.FOREIGN_CLONES}
        else []
    )
    try:
        findings = await _collect(
            command, options, repo=repo, pr_source=pr_source, session_bases=session_bases, progress=progress
        )
    except (GitError, OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error

    _print_findings(command, options, findings)
    multiple = len(domains) > 1
    foreign_prunable = _Domain.FOREIGN_CLONES in domains and any(
        isinstance(i, PrunableForeignClone) for i in findings.scan.foreign_clones
    )
    if not options.apply:
        if _any_prunable(command, findings):
            print(f"{'\n' if multiple else ''}{command.dry_run_hint}")
        if foreign_prunable:
            print("Foreign clones are prunable too; pass --prune --delete-foreign-clones to remove them.")
        return

    if multiple:
        print()
    if not await _apply(command, options, findings, session_bases=session_bases, progress=progress):
        raise SystemExit(1)


app = typer.Typer(help=__doc__, add_completion=False)

_RepoOption = Annotated[Path, typer.Option("--repo", help="a path inside the repo")]
_RootOption = Annotated[Path, typer.Option("--output-user-root", help="Bazel output user root")]
_AllOption = Annotated[bool, typer.Option("--all", help="show all items, including kept repo-cache entries")]
_NoPrsOption = Annotated[bool, typer.Option("--no-prs", help="skip the GitHub PR cross-check (git signals only)")]
_SizesOption = Annotated[bool, typer.Option("--sizes", help="calculate base sizes with du (potentially slow)")]
_VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="show detailed scan and repo-cache output")]
_WorkersOption = Annotated[
    int,
    typer.Option(
        "--workers",
        min=1,
        help="worker threads for worktree/branch/foreign-clone classification and for removing worktrees, output bases, repo-cache entries and foreign clones (the right count depends on the machine and disk)",
    ),
]


def _configure_logging(*, verbose: bool) -> None:
    # The compact live progress indicator (_ProgressReporter) is the default on-TTY signal for
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
    workers: _WorkersOption = workspace_scan.DEFAULT_CLASSIFY_WORKERS,
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
    workers: _WorkersOption = workspace_scan.DEFAULT_CLASSIFY_WORKERS,
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
    workers: _WorkersOption = workspace_scan.DEFAULT_CLASSIFY_WORKERS,
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
    workers: _WorkersOption = workspace_scan.DEFAULT_CLASSIFY_WORKERS,
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


def _raise_keyboard_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> None:
    """Run the CLI through its importable wheel entry point."""
    # asyncio.run installs its own SIGINT handler unless the current one is the default; that
    # handler only cancels the main task, which cannot take effect while a scan blocks the loop
    # thread, so Ctrl+C would need a second press. This one interrupts immediately.
    signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
    try:
        app(args=_with_default_command(sys.argv[1:] if argv is None else argv))
    except git_repo.INSPECTION_ERRORS as error:
        print(f"workspace-gc: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

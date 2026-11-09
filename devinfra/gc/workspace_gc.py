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
import threading
from collections import Counter
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
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
from devinfra.gc.branch_gc import BranchClassification, PrunableBranch, RemovedBranch, RetainedBranch, SkippedBranch
from devinfra.gc.foreign_clone_gc import (
    ForeignCloneClassification,
    PrunableForeignClone,
    PrunedForeignClone,
    RetainedForeignClone,
)
from devinfra.gc.git_repo import GitError
from devinfra.gc.output_base_gc import DeletedBase, FailedBase, PrunableBase, SkippedBase, default_output_user_root
from devinfra.gc.pull_request import PrInfo
from devinfra.gc.repo_contents_cache_gc import DeletedEntry, EntryInspection, FailedEntry, PrunableEntry, SkippedEntry
from devinfra.gc.scan_progress import NULL_PROGRESS, ProgressCategory, ProgressSink
from devinfra.gc.verdict import verdict_of
from devinfra.gc.workspace_scan import WorkspaceScan
from devinfra.gc.worktree_gc import (
    Classification,
    PrunableWorktree,
    RemovedWorktree,
    RetainedWorktree,
    ReviewWorktree,
    SkippedWorktree,
)

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_USER_ROOT = default_output_user_root()
_PROGRESS_COUNT_COLUMNS: dict[ProgressCategory, str] = {"PRUNE": "prune", "KEEP": "keep", "REVIEW": "review"}
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


def _gather_prs(repo: Path, *, no_prs: bool, extra_branches: Set[str] = frozenset()) -> dict[str, PrInfo]:
    if no_prs:
        return {}
    return github_prs.pr_states(repo, workspace_scan.pr_branch_candidates(repo) | extra_branches)


@dataclass(slots=True)
class _PhaseProgress:
    total: int
    counts: dict[ProgressCategory, int]


class _ProgressReporter:
    """Live per-phase progress rendered as a borderless table: one row per scan phase, with
    its prune/keep/review counts in aligned columns and a bar; a no-op off a TTY (`rich.live.Live`
    detects this itself).

    A scan over hundreds of worktrees and branches can take minutes with nothing else to show
    for it in the meantime — this renders in place instead of the wall of `logger.info` lines
    underneath (still there, for `--verbose`). Updates take a lock, which the classification
    worker pools rely on. `transient=True` clears the table on completion — the CLI's own
    report print, right after, is the record of the outcome.

    Used as a context manager around each scan call: `Live` owns the terminal's bottom region
    while live, so any other output must happen with it stopped, not interleaved. Each entry
    starts with an empty table.
    """

    def __init__(self) -> None:
        self._console = Console(stderr=True)
        self._phases: dict[str, _PhaseProgress] = {}
        self._live: Live | None = None
        self._lock = threading.Lock()

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
        for _ in _PROGRESS_COUNT_COLUMNS:
            table.add_column(justify="right")
        table.add_column()
        table.add_column(justify="right")
        table.add_row("", *(Text(label, style="dim") for label in _PROGRESS_COUNT_COLUMNS.values()), "", "")
        for phase, state in self._phases.items():
            done = sum(state.counts.values())
            table.add_row(
                phase,
                *(str(state.counts[category]) for category in _PROGRESS_COUNT_COLUMNS),
                ProgressBar(total=state.total, completed=done, width=_PROGRESS_BAR_WIDTH),
                f"{done}/{state.total}",
            )
        return table

    def _refresh(self) -> None:
        assert self._live is not None
        self._live.update(self.render())

    def start_phase(self, phase: str, total: int) -> None:
        with self._lock:
            self._phases[phase] = _PhaseProgress(total, {"PRUNE": 0, "KEEP": 0, "REVIEW": 0})
            self._refresh()

    def record(self, phase: str, category: ProgressCategory) -> None:
        with self._lock:
            self._phases[phase].counts[category] += 1
            self._refresh()


def _scan(
    repo: Path,
    *,
    prs: dict[str, PrInfo],
    output_user_root: Path | None,
    session_bases: Sequence[Path],
    progress: ProgressSink,
    workers: int,
    foreign_clone_roots: Sequence[Path] = (),
) -> WorkspaceScan:
    pg = git_repo.open_repo(repo)
    return workspace_scan.scan_workspace(
        repo,
        main=git_repo.main_ref(pg),
        default_branch=git_repo.default_branch_name(pg),
        pr_states=prs,
        session_bases=session_bases,
        active_path=_active_worktree(repo),
        output_user_root=output_user_root,
        foreign_clone_roots=foreign_clone_roots,
        workers=workers,
        progress=progress,
    )


def _relative_to(path: Path, start: Path) -> str:
    return os.path.relpath(path, start)


async def _run_bounded[T, R](items: Sequence[T], function: Callable[[T], R], *, workers: int) -> list[R]:
    """Run blocking `function` over `items` on worker threads, `workers` at a time; results are in
    `items` order. `function` must be independent per item."""
    semaphore = asyncio.Semaphore(workers)

    async def run_one(item: T) -> R:
        async with semaphore:
            return await asyncio.to_thread(function, item)

    return list(await asyncio.gather(*(run_one(item) for item in items)))


async def _apply_worktree_removals(
    repo: Path, worktrees: list[Classification], *, prs: dict[str, PrInfo], workers: int
) -> bool:
    """Remove every prunable worktree that is still prunable now; returns whether all succeeded."""
    candidates = [item for item in worktrees if isinstance(item, PrunableWorktree)]
    if not candidates:
        return True
    logger.info("Removing %d worktrees", len(candidates))
    main_path = git_repo.main_worktree(repo)
    active_path = _active_worktree(repo)
    live = worktree_gc.processes_by_worktree(item.worktree.path for item in candidates)

    def remove_one(item: PrunableWorktree) -> worktree_gc.RemovalResult:
        logger.info("Removing worktree %s", item.worktree.path)
        return worktree_gc.remove_if_still_prunable(
            repo,
            item.worktree,
            main=item.main,
            pr_states=prs,
            main_path=main_path,
            active_path=active_path,
            live_pids=live.get(item.worktree.path, []),
        )

    results = await _run_bounded(candidates, remove_one, workers=workers)
    for result in results:
        if isinstance(result, RemovedWorktree):
            print(f"REMOVED worktree {_short(result.path)}")
        elif isinstance(result, SkippedWorktree):
            print(f"SKIPPED worktree {_short(result.path)}: {result.reason}", file=sys.stderr)
        else:
            print(f"FAILED worktree {_short(result.path)}: {result.error}", file=sys.stderr)
    removed = sum(isinstance(result, RemovedWorktree) for result in results)
    skipped = sum(isinstance(result, SkippedWorktree) for result in results)
    failed = len(results) - removed - skipped
    print(f"Worktrees: {removed} removed, {skipped} skipped, {failed} failed")
    return skipped == 0 and failed == 0


def _apply_branch_deletions(repo: Path, branches: list[BranchClassification]) -> bool:
    """Delete every prunable branch that has not moved since it was classified; returns whether
    all of them succeeded."""
    candidates = [item for item in branches if isinstance(item, PrunableBranch)]
    if not candidates:
        return True
    logger.info("Deleting %d branches", len(candidates))
    results = []
    for index, item in enumerate(candidates, start=1):
        logger.info("Deleting branch %d/%d %s", index, len(candidates), item.branch.name)
        results.append(branch_gc.delete_branch(repo, item.branch.name, expected_tip=item.tip))
    for result in results:
        if isinstance(result, RemovedBranch):
            print(f"DELETED branch {result.name}")
        elif isinstance(result, SkippedBranch):
            print(f"SKIPPED branch {result.name}: {result.reason}", file=sys.stderr)
        else:
            print(f"FAILED branch {result.name}: {result.error}", file=sys.stderr)
    deleted = sum(isinstance(result, RemovedBranch) for result in results)
    skipped = sum(isinstance(result, SkippedBranch) for result in results)
    failed = len(results) - deleted - skipped
    print(f"Branches: {deleted} deleted, {skipped} skipped, {failed} failed")
    return skipped == 0 and failed == 0


@dataclass(frozen=True, slots=True)
class _BaseDeletions:
    ok: bool
    deleted: frozenset[Path]


async def _apply_base_deletions(
    candidates: Sequence[PrunableBase], *, output_user_root: Path, workers: int
) -> _BaseDeletions:
    """Delete the given prunable output bases; `delete_prunable_bases` revalidates each one under
    its Bazel lock, so the caller's scan needs no refresh beyond what it already knows."""
    if not candidates:
        return _BaseDeletions(ok=True, deleted=frozenset())
    free_before = shutil.disk_usage(output_user_root).free
    results = await _run_bounded(candidates, output_base_gc.delete_prunable_base, workers=workers)
    free_change = shutil.disk_usage(output_user_root).free - free_before
    for result in results:
        if isinstance(result, DeletedBase):
            print(f"DELETED base {_short(result.path)}")
        elif isinstance(result, SkippedBase):
            print(f"SKIPPED base {_short(result.path)}: {result.reason}", file=sys.stderr)
        else:
            quarantine = f"; quarantine={result.quarantine}" if result.quarantine is not None else ""
            print(f"FAILED base {_short(result.path)}: {result.error}{quarantine}", file=sys.stderr)
    deleted = sum(isinstance(result, DeletedBase) for result in results)
    skipped = sum(isinstance(result, SkippedBase) for result in results)
    failed = sum(isinstance(result, FailedBase) for result in results)
    print(
        f"Bases: {deleted} deleted, {skipped} skipped, {failed} failed; "
        f"free-space change {humanize.naturalsize(free_change, binary=True)}"
    )
    return _BaseDeletions(
        ok=skipped == 0 and failed == 0,
        deleted=frozenset(result.path for result in results if isinstance(result, DeletedBase)),
    )


async def _apply_repo_cache_deletions(
    candidates: Sequence[PrunableEntry],
    *,
    output_user_root: Path,
    session_bases: Sequence[Path],
    verbose: bool,
    workers: int,
) -> bool:
    """Delete the given unreferenced repo-cache entries; returns whether all of them succeeded.

    `delete_prunable_entries` revalidates each entry under Bazel's cache lock against a fresh
    link pass, so the caller's classification needs no refresh.
    """
    if not candidates:
        return True
    contents = repo_contents_cache_gc.repo_contents_cache(output_user_root)
    free_before = shutil.disk_usage(contents).free
    trashed = await asyncio.to_thread(
        repo_contents_cache_gc.trash_prunable_entries,
        candidates,
        contents=contents,
        output_user_root=output_user_root,
        session_bases=session_bases,
    )
    results = [
        *trashed.results,
        *await _run_bounded(trashed.trashed, lambda item: repo_contents_cache_gc.empty_trash(*item), workers=workers),
    ]
    free_change = shutil.disk_usage(contents).free - free_before
    skip_reasons: Counter[str] = Counter()
    for result in results:
        name = _relative_to(result.path, contents)
        if isinstance(result, DeletedEntry):
            if verbose:
                print(f"DELETED repo-cache entry {name}")
        elif isinstance(result, SkippedEntry):
            skip_reasons[result.reason] += 1
            if verbose:
                print(f"SKIPPED repo-cache entry {name}: {result.reason}", file=sys.stderr)
        else:
            trash = f"; trash={result.trash}" if result.trash is not None else ""
            print(f"FAILED repo-cache entry {name}: {result.error}{trash}", file=sys.stderr)
    if not verbose:
        for reason, count in skip_reasons.items():
            print(f"SKIPPED {count} repo-cache entries: {reason}", file=sys.stderr)
    deleted = sum(isinstance(result, DeletedEntry) for result in results)
    skipped = sum(isinstance(result, SkippedEntry) for result in results)
    failed = sum(isinstance(result, FailedEntry) for result in results)
    print(
        f"Repo cache: {deleted} deleted, {skipped} skipped, {failed} failed; "
        f"free-space change {humanize.naturalsize(free_change, binary=True)}"
    )
    return skipped == 0 and failed == 0


async def _apply_foreign_clone_deletions(
    repo: Path, output_user_root: Path, session_bases: Sequence[Path], prs: dict[str, PrInfo], workers: int
) -> bool:
    """Remove every prunable foreign clone; returns whether all of them succeeded.

    Fresh discovery + classification right here is the revalidation: nothing computed before
    this call is reused, so a clone that turned dirty (or gained an open PR) in the meantime is
    never passed to `prune_foreign_clone`.
    """
    roots = workspace_scan.foreign_clone_roots(repo, output_user_root, session_bases=session_bases)
    active_path = _active_worktree(repo)

    def classify_one(root: Path) -> foreign_clone_gc.ForeignCloneClassification:
        return foreign_clone_gc.classify_foreign_clone(root, pr_states=prs, active_path=active_path)

    classifications = workspace_scan.parallel_classify(
        roots,
        lambda: classify_one,
        phase="foreign_clones",
        noun="foreign clone",
        progress=NULL_PROGRESS,
        category_of=verdict_of,
        describe=str,
        workers=workers,
    )
    candidates = [item.clone for item in classifications if isinstance(item, PrunableForeignClone)]
    if not candidates:
        return True
    logger.info("Removing %d foreign clones", len(candidates))
    results = await _run_bounded(candidates, foreign_clone_gc.prune_foreign_clone, workers=workers)
    for result in results:
        if isinstance(result, PrunedForeignClone):
            print(f"REMOVED foreign clone {_short(result.root)}")
        else:
            print(f"FAILED foreign clone {_short(result.root)}: {result.error}", file=sys.stderr)
    removed = sum(isinstance(result, PrunedForeignClone) for result in results)
    failed = len(results) - removed
    print(f"Foreign clones: {removed} removed, {failed} failed")
    return failed == 0


async def run_worktrees(repo: Path, *, show_all: bool, no_prs: bool, prune: bool, workers: int) -> None:
    progress = _ProgressReporter()
    try:
        prs = _gather_prs(repo, no_prs=no_prs)
        with progress:
            scan = _scan(repo, prs=prs, output_user_root=None, session_bases=(), progress=progress, workers=workers)
    except GitError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
    print(render_worktrees(scan.worktrees, include_kept=show_all))
    if not prune:
        if any(isinstance(item, PrunableWorktree) for item in scan.worktrees):
            print("Dry run only; pass --prune to remove the prunable worktrees.")
        return
    if not await _apply_worktree_removals(repo, scan.worktrees, prs=prs, workers=workers):
        raise SystemExit(1)


async def run_bases(
    repo: Path, *, output_user_root: Path, show_all: bool, no_prs: bool, sizes: bool, delete: bool, workers: int
) -> None:
    # Inspect the bases first: that filesystem pass alone decides every PRUNE/KEEP verdict.
    # Only the "workspace is a prunable worktree" annotation needs git, and only for the few
    # worktrees that are actually a base's workspace — so the PR query is scoped to their
    # branches instead of all of them, and no branch is ever classified.
    session_bases = output_base_gc.session_env_output_bases()
    try:
        with _ProgressReporter() as progress:
            bases = list(
                output_base_gc.scan_output_user_root(output_user_root, session_bases=session_bases, progress=progress)
            )
        prs = {} if no_prs else github_prs.pr_states(repo, workspace_scan.base_workspace_branches(repo, bases))
        with _ProgressReporter() as progress:
            bases = workspace_scan.annotate_bases(
                repo,
                bases,
                main=git_repo.main_ref(git_repo.open_repo(repo)),
                pr_states=prs,
                active_path=_active_worktree(repo),
                workers=workers,
                progress=progress,
            )
    except (GitError, OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
    print(output_base_gc.render_report(bases, include_kept=show_all, include_sizes=sizes))
    if not delete:
        if any(isinstance(item, PrunableBase) for item in bases):
            print("Dry run only; pass --delete to remove the prunable bases.")
        return
    candidates = [item for item in bases if isinstance(item, PrunableBase)]
    if not (await _apply_base_deletions(candidates, output_user_root=output_user_root, workers=workers)).ok:
        raise SystemExit(1)


async def run_repo_cache(
    *, output_user_root: Path, show_all: bool, sizes: bool, delete: bool, verbose: bool, workers: int
) -> None:
    # Entries are classified against every base's PRUNE/KEEP/REVIEW verdict, which the
    # filesystem alone decides; no worktree, branch, or PR state can change an entry's row.
    contents = repo_contents_cache_gc.repo_contents_cache(output_user_root)
    session_bases = output_base_gc.session_env_output_bases()
    try:
        with _ProgressReporter() as progress:
            bases = output_base_gc.scan_output_user_root(
                output_user_root, session_bases=session_bases, progress=progress
            )
            entries = repo_contents_cache_gc.scan_repo_contents_cache(contents, bases, progress=progress)
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
    print(
        repo_contents_cache_gc.render_report(
            entries, contents=contents, include_kept=show_all, include_sizes=sizes, include_prunable=verbose
        )
    )
    if not delete:
        if any(isinstance(item, PrunableEntry) for item in entries):
            print("Dry run only; pass --delete to remove the unreferenced entries.")
        return
    candidates = [item for item in entries if isinstance(item, PrunableEntry)]
    if not await _apply_repo_cache_deletions(
        candidates, output_user_root=output_user_root, session_bases=session_bases, verbose=verbose, workers=workers
    ):
        raise SystemExit(1)


async def run_all(
    repo: Path,
    *,
    output_user_root: Path,
    show_all: bool,
    no_prs: bool,
    sizes: bool,
    prune: bool,
    delete_foreign_clones: bool,
    workers: int,
    verbose: bool = False,
) -> None:
    progress = _ProgressReporter()
    session_bases = output_base_gc.session_env_output_bases()
    contents = repo_contents_cache_gc.repo_contents_cache(output_user_root)
    try:
        foreign_roots = workspace_scan.foreign_clone_roots(repo, output_user_root, session_bases=session_bases)
        extra_branches = workspace_scan.foreign_clone_branch_candidates(foreign_roots)
        prs = _gather_prs(repo, no_prs=no_prs, extra_branches=extra_branches)
        with progress:
            scan = _scan(
                repo,
                prs=prs,
                output_user_root=output_user_root,
                session_bases=session_bases,
                progress=progress,
                workers=workers,
                foreign_clone_roots=foreign_roots,
            )
            entries: list[EntryInspection] = repo_contents_cache_gc.scan_repo_contents_cache(
                contents, scan.bases, progress=progress
            )
    except (GitError, OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error

    print("# Worktrees")
    print(render_worktrees(scan.worktrees, include_kept=show_all))
    print("\n# Branches")
    print(render_branches(scan.branches, include_kept=show_all))
    print("\n# Bazel output bases")
    print(output_base_gc.render_report(scan.bases, include_kept=show_all, include_sizes=sizes))
    print("\n# Repo contents cache")
    print(
        repo_contents_cache_gc.render_report(
            entries, contents=contents, include_kept=show_all, include_sizes=sizes, include_prunable=verbose
        )
    )
    print("\n# Foreign clones")
    print(render_foreign_clones(scan.foreign_clones, include_kept=show_all))

    foreign_prunable = any(isinstance(i, PrunableForeignClone) for i in scan.foreign_clones)
    if not prune:
        prunable = (
            any(isinstance(i, PrunableWorktree) for i in scan.worktrees)
            or any(isinstance(i, PrunableBranch) for i in scan.branches)
            or any(isinstance(i, PrunableBase) for i in scan.bases)
            or any(isinstance(i, PrunableEntry) for i in entries)
        )
        if prunable:
            print(
                "\nDry run only; pass --prune to remove all prunable worktrees, branches, bases, "
                "and repo-cache entries."
            )
        if foreign_prunable:
            print("Foreign clones are prunable too; pass --prune --delete-foreign-clones to remove them.")
        return

    print()

    def rescan_branches() -> list[BranchClassification]:
        pg = git_repo.open_repo(repo)
        with progress:
            return workspace_scan.scan_branches(
                repo,
                worktrees=scan.worktrees,
                main=git_repo.main_ref(pg),
                default_branch=git_repo.default_branch_name(pg),
                pr_states=prs,
                workers=workers,
                progress=progress,
            )

    # Remove worktrees first so branches they hold are freed and their bases orphan, and bases
    # before the repo cache so entries only they referenced become unreferenced. Worktrees reuse
    # the scan above (`git worktree remove` itself refuses a tree that turned dirty); branches
    # are re-classified because the removals change who holds them. Every step runs regardless
    # of an earlier one's failures — each domain is independent — and the combined outcome
    # decides the process exit status once, at the end.
    worktrees_ok = await _apply_worktree_removals(repo, scan.worktrees, prs=prs, workers=workers)
    branches_ok = _apply_branch_deletions(repo, rescan_branches())
    # Bases are rescanned because removing worktrees orphans bases the first scan kept. Cache
    # entries need no rescan: one is newly unreferenced exactly when every base linking to it
    # was just deleted.
    base_candidates = [
        item
        for item in output_base_gc.scan_output_user_root(output_user_root, session_bases=session_bases)
        if isinstance(item, PrunableBase)
    ]
    base_deletions = await _apply_base_deletions(base_candidates, output_user_root=output_user_root, workers=workers)
    repo_cache_ok = await _apply_repo_cache_deletions(
        repo_contents_cache_gc.unreferenced_after_deleting(entries, base_deletions.deleted),
        output_user_root=output_user_root,
        session_bases=session_bases,
        verbose=verbose,
        workers=workers,
    )
    bases_ok = base_deletions.ok
    foreign_ok = True
    if delete_foreign_clones:
        foreign_ok = await _apply_foreign_clone_deletions(repo, output_user_root, session_bases, prs, workers)
    elif foreign_prunable:
        print("Foreign clones were left alone; pass --delete-foreign-clones to remove them too.")
    if not (worktrees_ok and branches_ok and bases_ok and repo_cache_ok and foreign_ok):
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
    asyncio.run(
        run_all(
            repo,
            verbose=verbose,
            output_user_root=output_user_root,
            show_all=show_all,
            no_prs=no_prs,
            sizes=sizes,
            prune=prune,
            delete_foreign_clones=delete_foreign_clones,
            workers=workers,
        )
    )


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
    asyncio.run(run_worktrees(repo, show_all=show_all, no_prs=no_prs, prune=prune, workers=workers))


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
    asyncio.run(
        run_bases(
            repo,
            output_user_root=output_user_root,
            show_all=show_all,
            no_prs=no_prs,
            sizes=sizes,
            delete=delete,
            workers=workers,
        )
    )


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
    asyncio.run(
        run_repo_cache(
            output_user_root=output_user_root,
            show_all=show_all,
            sizes=sizes,
            delete=delete,
            verbose=verbose,
            workers=workers,
        )
    )


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

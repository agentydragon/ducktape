"""The single joint scan over local development state — worktrees, branches, output bases.

The three domains are coupled, so they are classified in one flow rather than three
independent passes: worktrees are the root; a branch's fate depends on the worktree holding
it, and a base's on whether its workspace (a worktree) survives. `scan_workspace` classifies
worktrees first, then branches against that result, then bases (annotating a retained base
whose workspace is a prunable worktree). It is network-free — PR state is injected — so the
CLI owns GitHub access and this stays unit-testable offline.

`annotate_bases` is the bases-only path. A base's own PRUNE/KEEP verdict is decided by
`output_base_gc` from the filesystem alone; the joint scan is needed only for the annotation
on a *retained* base whose workspace is a prunable worktree. So the caller inspects the bases
first and this classifies just the handful of worktrees that are some base's workspace —
never the whole repo, and never any branch. On a checkout with a hundred worktrees that is
the difference between five seconds and forty.
"""

from __future__ import annotations

import dataclasses
import logging
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from devinfra.gc import branch_gc, foreign_clone_gc, git_repo, output_base_gc, worktree_gc
from devinfra.gc.branch_gc import BranchClassification, Holder, MainCheckout, PrunableBranch, RetainedBranch
from devinfra.gc.foreign_clone_gc import ForeignCloneClassification
from devinfra.gc.output_base_gc import Inspection, RetainedBase
from devinfra.gc.pull_request import PrInfo
from devinfra.gc.scan_progress import NULL_PROGRESS, ProgressCategory, ProgressSink
from devinfra.gc.worktree_gc import Classification, PrunableWorktree, RetainedWorktree

logger = logging.getLogger(__name__)

# pygit2/git calls release the GIL, so threads give real concurrency; configurable (the CLI
# exposes `--workers`) since the right count depends on the machine and disk, not the code.
DEFAULT_CLASSIFY_WORKERS = 8


@dataclass(frozen=True, slots=True)
class WorkspaceScan:
    worktrees: list[Classification]
    branches: list[BranchClassification]
    bases: list[Inspection]
    foreign_clones: list[ForeignCloneClassification] = field(default_factory=list)


def pr_branch_candidates(repo: Path) -> set[str]:
    """Branch names worth a GitHub PR lookup: every local branch plus any checked out."""
    pg = git_repo.open_repo(git_repo.main_worktree(repo))
    names = set(branch_gc.local_branches(pg))
    names.update(wt.branch for wt in git_repo.list_worktrees(repo) if wt.branch)
    return names


def _resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def _resolved(paths: set[Path]) -> set[Path]:
    return {resolved for path in paths if (resolved := _resolve(path)) is not None}


def _annotate_base(base: Inspection, prunable_workspaces: set[Path]) -> Inspection:
    """Flag a retained base whose workspace is a prunable worktree — it orphans once removed."""
    if not isinstance(base, RetainedBase) or base.workspace is None:
        return base
    try:
        workspace = base.workspace.resolve()
    except OSError:
        return base
    if workspace not in prunable_workspaces:
        return base
    return dataclasses.replace(base, reason=f"{base.reason} — workspace is a prunable worktree (prune it first)")


def _worktree_category(classification: Classification) -> ProgressCategory:
    if isinstance(classification, PrunableWorktree):
        return "PRUNE"
    if isinstance(classification, RetainedWorktree):
        return "KEEP"
    return "REVIEW"


def _branch_category(classification: BranchClassification) -> ProgressCategory:
    if isinstance(classification, PrunableBranch):
        return "PRUNE"
    if isinstance(classification, RetainedBranch):
        return "KEEP"
    return "REVIEW"


def foreign_clone_category(classification: ForeignCloneClassification) -> ProgressCategory:
    if isinstance(classification, foreign_clone_gc.PrunableForeignClone):
        return "PRUNE"
    if isinstance(classification, foreign_clone_gc.RetainedForeignClone):
        return "KEEP"
    return "REVIEW"


def parallel_classify[T, R](
    items: Sequence[T],
    make_classify_one: Callable[[], Callable[[T], R]],
    *,
    phase: str,
    noun: str,
    progress: ProgressSink,
    category_of: Callable[[R], ProgressCategory],
    describe: Callable[[T], str],
    workers: int,
) -> list[R]:
    """Classify `items` in up to `workers`-way parallel contiguous slices.

    `make_classify_one` is called once per *worker thread*, not once per item: a classifier
    that needs a shared per-slice resource — a single `pygit2.Repository`, say, since handles
    aren't shareable across threads — opens it once and reuses it across its whole slice; one
    with nothing to share (each item opens its own resources independently) just returns the
    same function every time.

    Real concurrency despite being threads, not processes: the pygit2/git calls each
    classifier makes release the GIL. Each worker classifies its own slice in order, so the
    flattened result stays in `items` order regardless of which slice finishes first;
    `progress` (thread-safe) is updated as each item completes, from whichever worker
    finishes it.
    """
    items = list(items)
    total = len(items)
    if total:
        progress.start_phase(phase, total)

    def classify_slice(args: tuple[int, list[T]]) -> list[R]:
        offset, slice_items = args
        classify_one = make_classify_one()
        results: list[R] = []
        for index, item in enumerate(slice_items, start=offset + 1):
            logger.info("Scanning %s %d/%d %s", noun, index, total, describe(item))
            result = classify_one(item)
            results.append(result)
            logger.info("Finished %s %d/%d %s", noun, index, total, describe(item))
            progress.record(phase, category_of(result))
        return results

    workers = min(workers, total)
    if workers <= 1:
        return classify_slice((0, items))
    step = -(-total // workers)  # ceil → `workers` contiguous slices
    slices = [(i, items[i : i + step]) for i in range(0, total, step)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [result for chunk in pool.map(classify_slice, slices) for result in chunk]


def _classify_branches(
    main_path: Path,
    names: list[str],
    *,
    main: str,
    default_branch: str,
    pr_states: dict[str, PrInfo],
    holder_for: Callable[[str], Holder],
    workers: int,
    progress: ProgressSink,
) -> list[BranchClassification]:
    def make_classify_one() -> Callable[[str], BranchClassification]:
        pg = git_repo.open_repo(main_path)

        def classify_one(name: str) -> BranchClassification:
            return branch_gc.classify_branch(
                name, pg=pg, main=main, default_branch=default_branch, pr=pr_states.get(name), holder=holder_for(name)
            )

        return classify_one

    return parallel_classify(
        names,
        make_classify_one,
        phase="branches",
        noun="branch",
        progress=progress,
        category_of=_branch_category,
        describe=str,
        workers=workers,
    )


def base_workspace_branches(repo: Path, bases: list[Inspection]) -> set[str]:
    """Branch names worth a PR lookup for a bases-only scan: those of base workspaces.

    Only a worktree that is some retained base's workspace can change a base's annotation,
    so the bases view queries PR state for those branches instead of every local branch.
    """
    workspaces = _retained_workspaces(bases)
    return {wt.branch for wt in _worktrees_at(repo, workspaces) if wt.branch}


def _retained_workspaces(bases: list[Inspection]) -> set[Path]:
    return _resolved(
        {base.workspace for base in bases if isinstance(base, RetainedBase) and base.workspace is not None}
    )


def _known_worktree_paths(repo: Path) -> set[Path]:
    return _resolved({wt.path for wt in git_repo.list_worktrees(repo)})


def foreign_clone_roots(repo: Path, output_user_root: Path) -> list[Path]:
    """Roots of foreign clones of `repo`'s project found among Bazel output-base workspaces.

    Filesystem-only (no network): re-scans output bases purely to learn candidate workspace
    paths, independent of the main scan's own later bases pass — cheap without `--sizes`, so
    duplicating it here is simpler than threading a shared scan through both call sites (the
    bases-only CLI path already does the analogous two-pass thing for its PR-annotation step).
    """
    bases = output_base_gc.scan_output_user_root(output_user_root)
    return foreign_clone_gc.discover_foreign_clones(
        _retained_workspaces(bases), known_paths=_known_worktree_paths(repo), repo=repo
    )


def foreign_clone_branch_candidates(roots: Sequence[Path]) -> set[str]:
    """Branch names worth a PR lookup across every worktree these foreign clones hold."""
    names: set[str] = set()
    for root in roots:
        try:
            names.update(wt.branch for wt in git_repo.list_worktrees(root) if wt.branch)
        except OSError, subprocess.CalledProcessError:
            continue
    return names


def _worktrees_at(repo: Path, workspaces: set[Path]) -> list[git_repo.Worktree]:
    """The linked worktrees sitting at one of `workspaces` (already resolved)."""
    if not workspaces:
        return []
    main_path = git_repo.main_worktree(repo)
    return [wt for wt in git_repo.list_worktrees(repo) if wt.path != main_path and _resolve(wt.path) in workspaces]


def annotate_bases(
    repo: Path,
    bases: list[Inspection],
    *,
    main: str,
    pr_states: dict[str, PrInfo],
    active_path: Path | None = None,
    proc_root: Path = Path("/proc"),
    workers: int = DEFAULT_CLASSIFY_WORKERS,
    progress: ProgressSink = NULL_PROGRESS,
) -> list[Inspection]:
    """Flag each retained base whose workspace is a prunable worktree.

    Same annotation `scan_workspace` applies, but it classifies only the worktrees that are
    some base's workspace — no other worktree, and no branch, can change the outcome.
    """
    candidates = _worktrees_at(repo, _retained_workspaces(bases))
    if not candidates:
        return bases

    main_path = git_repo.main_worktree(repo)
    live = worktree_gc.processes_by_worktree((wt.path for wt in candidates), proc_root=proc_root)

    def classify_one(wt: git_repo.Worktree) -> Classification:
        return worktree_gc.classify_worktree(
            wt,
            main=main,
            pr_states=pr_states,
            main_path=main_path,
            active_path=active_path,
            live_pids=live.get(wt.path, []),
        )

    classifications = parallel_classify(
        candidates,
        lambda: classify_one,
        phase="workspaces",
        noun="base workspace",
        progress=progress,
        category_of=_worktree_category,
        describe=lambda wt: str(wt.path),
        workers=workers,
    )
    prunable = _resolved(
        {wt.path for wt, c in zip(candidates, classifications, strict=True) if isinstance(c, PrunableWorktree)}
    )
    return [_annotate_base(base, prunable) for base in bases]


def scan_workspace(
    repo: Path,
    *,
    main: str,
    default_branch: str,
    pr_states: dict[str, PrInfo],
    active_path: Path | None = None,
    output_user_root: Path | None = None,
    foreign_clone_roots: Sequence[Path] = (),
    proc_root: Path = Path("/proc"),
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    workers: int = DEFAULT_CLASSIFY_WORKERS,
    progress: ProgressSink = NULL_PROGRESS,
) -> WorkspaceScan:
    main_path = git_repo.main_worktree(repo)
    pg = git_repo.open_repo(main_path)

    linked = [wt for wt in git_repo.list_worktrees(repo) if wt.path != main_path]
    logger.info("Scanning %d linked worktrees", len(linked))
    live = worktree_gc.processes_by_worktree((wt.path for wt in linked), proc_root=proc_root)

    def classify_worktree_one(wt: git_repo.Worktree) -> Classification:
        return worktree_gc.classify_worktree(
            wt,
            main=main,
            pr_states=pr_states,
            main_path=main_path,
            active_path=active_path,
            live_pids=live.get(wt.path, []),
        )

    worktrees = parallel_classify(
        linked,
        lambda: classify_worktree_one,
        phase="worktrees",
        noun="worktree",
        progress=progress,
        category_of=_worktree_category,
        describe=lambda wt: str(wt.path),
        workers=workers,
    )
    logger.info("Worktree scan complete: %d linked worktrees", len(linked))

    holders = branch_gc.branch_holders(repo)
    holder_by_path = {item.worktree.path: item for item in worktrees}

    def holder_for(name: str) -> Holder:
        holder_path = holders.get(name)
        if holder_path is None:
            return None
        if holder_path == main_path:
            return MainCheckout()
        return holder_by_path.get(holder_path)

    names = branch_gc.local_branches(pg)
    logger.info("Scanning %d local branches", len(names))
    branches = _classify_branches(
        main_path,
        names,
        main=main,
        default_branch=default_branch,
        pr_states=pr_states,
        holder_for=holder_for,
        workers=workers,
        progress=progress,
    )
    logger.info("Branch scan complete: %d local branches", len(branches))

    bases: list[Inspection] = []
    if output_user_root is not None:
        prunable_workspaces = _resolved(
            {item.worktree.path for item in worktrees if isinstance(item, PrunableWorktree)}
        )
        logger.info("Scanning Bazel output bases in %s", output_user_root)

        bases = [
            _annotate_base(base, prunable_workspaces)
            for base in output_base_gc.scan_output_user_root(
                output_user_root, proc_root=proc_root, mountinfo_path=mountinfo_path, progress=progress
            )
        ]
        logger.info("Bazel output-base scan complete: %d bases", len(bases))

    logger.info("Scanning %d foreign clones", len(foreign_clone_roots))

    def classify_foreign_clone_one(root: Path) -> ForeignCloneClassification:
        return foreign_clone_gc.classify_foreign_clone(
            root, pr_states=pr_states, active_path=active_path, proc_root=proc_root
        )

    foreign_clones = parallel_classify(
        foreign_clone_roots,
        lambda: classify_foreign_clone_one,
        phase="foreign_clones",
        noun="foreign clone",
        progress=progress,
        category_of=foreign_clone_category,
        describe=str,
        workers=workers,
    )
    logger.info("Foreign-clone scan complete: %d clones", len(foreign_clones))

    return WorkspaceScan(worktrees=worktrees, branches=branches, bases=bases, foreign_clones=foreign_clones)

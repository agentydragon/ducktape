"""Classify and safely remove foreign clones of the repo being scanned.

A Bazel output base records its workspace's absolute path, and that workspace does not have
to be a worktree the invoking repo knows about: an old scratch `git clone` of the same
project, or a worktree of one, produces its own output base but is invisible to
`git worktree list` run against the repo workspace-gc was invoked on. `output_base_gc`'s only
signal for such a base is "does the workspace path still exist" — it can't tell a live scratch
clone from an abandoned one, so it never leaves KEEP no matter how stale.

A candidate only counts once it shares an actual remote with the repo being scanned
(`git_repo.shares_a_remote` — any remote URL in common, on any host, under any remote name —
never GitHub-specific and never assuming either side calls it `origin`). That is what rules
out a directory that merely happens to share this machine's Bazel cache with an unrelated
project. Once that holds, classification reuses `worktree_gc` unchanged against the
candidate's own repository: every worktree it holds (its own primary checkout, plus anything
`git worktree list` finds registered against it — a scratch clone can itself host further
worktrees) must independently classify `PrunableWorktree` before the whole clone is prunable.
Removing a clone is `shutil.rmtree` on an independent git object store, not `git worktree
remove` — unlike a linked worktree of the repo being scanned, it can genuinely lose history if
something upstream of it was wrong, so a single un-prunable worktree anywhere inside holds
back the whole clone.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pygit2

from devinfra.gc import git_repo, worktree_gc
from devinfra.gc.git_repo import GitError, Worktree
from devinfra.gc.pull_request import PrInfo
from devinfra.gc.worktree_gc import PrunableWorktree, RetainedWorktree

logger = logging.getLogger(__name__)

# `classify_worktree` skips its "main checkout" protection for any worktree at this path. A
# foreign clone's own primary checkout is not the repo workspace-gc was invoked on, so unlike
# that repo's real main worktree it gets no free pass — it classifies on the same terms as
# everything else the clone holds.
_NEVER_A_CHECKOUT = Path("\0workspace-gc: no worktree is ever this path\0")


@dataclass(frozen=True, slots=True)
class ForeignClone:
    root: Path  # where its `.git` (or, for a linked worktree, the shared `.git` it points to) lives
    members: list[Worktree] = field(default_factory=list)  # every worktree it holds, root's own included


@dataclass(frozen=True, slots=True)
class PrunableForeignClone:
    clone: ForeignClone
    reason: str


@dataclass(frozen=True, slots=True)
class RetainedForeignClone:
    clone: ForeignClone
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewForeignClone:
    clone: ForeignClone
    reason: str


type ForeignCloneClassification = PrunableForeignClone | RetainedForeignClone | ReviewForeignClone


def _find_repo_root(path: Path) -> tuple[Path, pygit2.Repository] | None:
    """Walk up from `path` to the nearest ancestor (inclusive) that is a git repository root,
    returning it already open — the caller needs an open repository next anyway (to compare
    remotes), so this hands that handle over instead of making the caller reopen the same path.

    Covers a Bazel workspace that is a *subdirectory* of a clone — a vendored third_party
    checkout built as its own Bazel workspace but not its own git repo — by resolving it to
    the clone that owns it, rather than treating it as its own (nonexistent) foreign clone.

    A `.git` entry alone isn't proof: an interrupted `git init` (or a stray one, e.g. directly
    at `/tmp`) leaves a `.git` directory that doesn't actually open as a repository, so this
    verifies with pygit2 before accepting a candidate rather than crashing every later step
    that assumes a valid repo.
    """
    for candidate in (path, *path.parents):
        git_entry = candidate / ".git"
        if not (git_entry.is_file() or git_entry.is_dir()):
            continue
        try:
            pg = pygit2.Repository(os.fspath(candidate))
        except pygit2.GitError:
            continue
        return candidate, pg
    return None


def discover_foreign_clones(candidate_workspaces: set[Path], *, known_paths: set[Path], repo: Path) -> list[Path]:
    """Roots of every foreign clone of `repo`'s project found among `candidate_workspaces`.

    Both path sets must already be resolved (`Path.resolve()`). A workspace under
    `known_paths` (a worktree of `repo` itself) is never a foreign clone — that exclusion is
    what "foreign" means here.
    """
    repo_remotes = git_repo.remote_urls(git_repo.open_repo(repo))
    roots: set[Path] = set()
    for workspace in sorted(candidate_workspaces):
        if workspace in known_paths:
            continue
        found = _find_repo_root(workspace)
        if found is None:
            continue
        candidate_path, pg = found
        if candidate_path in known_paths:
            continue
        if not git_repo.remote_urls(pg) & repo_remotes:
            continue
        try:
            roots.add(git_repo.main_worktree(candidate_path))
        except (OSError, subprocess.CalledProcessError) as error:
            logger.warning("cannot resolve the main worktree of candidate clone %s: %s", candidate_path, error)
    return sorted(roots)


def classify_foreign_clone(
    root: Path, *, pr_states: dict[str, PrInfo], active_path: Path | None = None, proc_root: Path = Path("/proc")
) -> ForeignCloneClassification:
    """Classify one foreign clone: prunable only when every worktree it holds is.

    Reuses `classify_worktree` unchanged against each of the clone's own worktrees — the only
    new input is which repository root to open, not new classification logic. Ancestry checks
    run against *that* clone's own `origin/<default-branch>`; if it is stale, the worst case is
    a false REVIEW/KEEP, never a false PRUNE.
    """
    empty = ForeignClone(root)
    try:
        members = git_repo.list_worktrees(root)
    except (OSError, subprocess.CalledProcessError) as error:
        return ReviewForeignClone(empty, f"cannot list its worktrees: {error}")
    if not members:
        return ReviewForeignClone(empty, "git worktree list returned nothing")
    try:
        main = git_repo.main_ref(git_repo.open_repo(root))
    except GitError as error:
        return ReviewForeignClone(ForeignClone(root, members), str(error))

    clone = ForeignClone(root, members)
    live = worktree_gc.processes_by_worktree((member.path for member in members), proc_root=proc_root)
    classifications = [
        worktree_gc.classify_worktree(
            member,
            main=main,
            pr_states=pr_states,
            main_path=_NEVER_A_CHECKOUT,
            active_path=active_path,
            live_pids=live.get(member.path, []),
        )
        for member in members
    ]
    blocking = next((item for item in classifications if not isinstance(item, PrunableWorktree)), None)
    if blocking is None:
        return PrunableForeignClone(clone, "every worktree it holds is prunable")
    if isinstance(blocking, RetainedWorktree):
        return RetainedForeignClone(clone, f"{blocking.worktree.path}: {blocking.reason}")
    return ReviewForeignClone(clone, f"{blocking.worktree.path}: {blocking.reason}")


@dataclass(frozen=True, slots=True)
class PrunedForeignClone:
    root: Path


@dataclass(frozen=True, slots=True)
class FailedForeignClone:
    root: Path
    error: str


type ForeignCloneRemovalResult = PrunedForeignClone | FailedForeignClone


def prune_foreign_clone(clone: ForeignClone) -> ForeignCloneRemovalResult:
    """Remove every non-root worktree properly (`git worktree remove`), then the root's own
    directory — safe once its worktrees are gone, since nothing else depends on it.

    The caller re-scans and passes only a still-prunable `ForeignClone` here, the same
    revalidate-then-act contract `worktree_gc.remove_worktree` documents; `git worktree remove`
    itself refuses a member that turned dirty since, surfacing as `FailedForeignClone`.
    """
    for member in clone.members:
        if member.path == clone.root:
            continue
        result = worktree_gc.remove_worktree(clone.root, member.path)
        if isinstance(result, worktree_gc.FailedWorktree):
            return FailedForeignClone(clone.root, f"removing {member.path}: {result.error}")
    try:
        shutil.rmtree(clone.root)
    except OSError as error:
        return FailedForeignClone(clone.root, str(error))
    return PrunedForeignClone(clone.root)

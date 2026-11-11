"""Discover and classify foreign clones of the repo being scanned.

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
project. Once that holds, classification reuses `worktree_gc` against the candidate's own
repository so the report can describe its registered worktrees. A foreign clone is still an
independent Git repository: worktree status cannot account for all of its refs, commits, or
other repository data, so even a clone whose registered worktrees look prunable requires
human review and is never removed automatically.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pygit2

from devinfra.gc import git_repo, worktree_gc
from devinfra.gc.git_repo import GitError, Worktree
from devinfra.gc.pull_request import PrInfo
from devinfra.gc.verdict import Retained, Review
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
class RetainedForeignClone(Retained):
    clone: ForeignClone
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewForeignClone(Review):
    clone: ForeignClone
    reason: str


type ForeignCloneClassification = RetainedForeignClone | ReviewForeignClone


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
        except git_repo.INSPECTION_ERRORS as error:
            logger.warning("Cannot inspect candidate repository %s: %s", candidate, error)
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
        try:
            if not git_repo.remote_urls(pg) & repo_remotes:
                continue
            roots.add(git_repo.main_worktree(candidate_path))
        except git_repo.INSPECTION_ERRORS as error:
            logger.warning("Cannot inspect candidate clone %s: %s", candidate_path, error)
    return sorted(roots)


def classify_foreign_clone(
    root: Path, *, pr_states: dict[str, PrInfo], active_path: Path | None = None, proc_root: Path = Path("/proc")
) -> ForeignCloneClassification:
    """Classify one clone, protecting the entire clone if any inspection fails."""
    try:
        return _classify_foreign_clone(root, pr_states=pr_states, active_path=active_path, proc_root=proc_root)
    except git_repo.INSPECTION_ERRORS as error:
        detail = f"{type(error).__name__}: {error}"
        logger.warning("Cannot inspect foreign clone %s: %s", root, detail)
        return ReviewForeignClone(ForeignClone(root), f"cannot inspect: {detail}")


def _classify_foreign_clone(
    root: Path, *, pr_states: dict[str, PrInfo], active_path: Path | None, proc_root: Path
) -> ForeignCloneClassification:
    """Classify one foreign clone for reporting; it is never safe to prune automatically.

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
        return ReviewForeignClone(
            clone,
            "all registered worktrees appear prunable, but this independent Git repository may hold other refs, commits, or data",
        )
    reason = f"{blocking.worktree.path}: {worktree_gc.describe_reason(blocking)}"
    if isinstance(blocking, RetainedWorktree):
        return RetainedForeignClone(clone, reason)
    return ReviewForeignClone(clone, reason)

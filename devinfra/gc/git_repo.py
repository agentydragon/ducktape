"""Git repository queries shared across the workspace-gc scanners.

Repo-level plumbing — running git, enumerating worktrees, resolving the default branch — and
the content-in-main ancestry test, used by both the worktree and branch classifiers. It does
not belong to either domain, so it lives here rather than in `worktree_gc` or `branch_gc`.

Worktree enumeration uses `git worktree list --porcelain` (pygit2's `list_worktrees()` gives
only linked-worktree names, not the main one or branches). Content queries use pygit2.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pygit2

from devinfra.gc.async_io import run_blocking, run_process

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    """A git invocation failed in a way that blocks classification."""


# libgit2 reports missing objects as KeyError and invalid refs as ValueError, in
# addition to GitError. Classifiers turn these data/IO failures into REVIEW.
INSPECTION_ERRORS = (GitError, pygit2.GitError, KeyError, ValueError, OSError, subprocess.SubprocessError)


def open_repo(path: Path) -> pygit2.Repository:
    """Open `path` as a repository, normalizing pygit2's own `GitError` (a plain `Exception`,
    so it slips past every `except GitError` here that means this module's `GitError`) into
    the one exception type callers actually catch."""
    try:
        return pygit2.Repository(os.fspath(path))
    except pygit2.GitError as error:
        raise GitError(f"cannot open {path} as a git repository: {error}") from error


async def git(
    repo: Path,
    *args: str | os.PathLike[str],
    check: bool = True,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    interruptible: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run git asynchronously, reaping it on cancellation; admitted writes finish."""
    command = ["git", "-C", os.fspath(repo), *(os.fspath(arg) for arg in args)]
    result = await run_process(
        *command, input_text=input_text, env=dict(env) if env is not None else None, interruptible=interruptible
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
    return result


async def git_out(repo: Path, *args: str | os.PathLike[str]) -> str:
    return (await git(repo, *args)).stdout.strip()


_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


async def reflog_oids(repo: Path, ref: str, tip: str) -> list[str] | None:
    """Inventory all available reflog commit IDs, failing closed on unreadable evidence."""
    env = {**os.environ, "LC_ALL": "C"}
    shown = await git(repo, "reflog", "show", "--format=%H", ref, check=False, env=env)
    if shown.returncode != 0:
        return None
    shown_oids = [line.strip() for line in shown.stdout.splitlines() if line.strip()]
    if any(_OID.fullmatch(oid) is None for oid in shown_oids):
        return None

    path_result = await git(repo, "rev-parse", "--path-format=absolute", "--git-path", f"logs/{ref}", check=False)
    if path_result.returncode != 0:
        return None
    log_path = Path(path_result.stdout.strip())
    try:
        raw_log = await run_blocking(log_path.read_bytes)
    except FileNotFoundError:
        if shown_oids:
            return None
        entries: list[tuple[str, str]] = []
    except OSError, UnicodeError:
        return None
    else:
        entries = []
        for line in raw_log.splitlines():
            # Git creates a linked worktree's initial HEAD reflog row without a
            # tab or message. Both forms still have OIDs, identity, time and zone.
            metadata, _separator, _message = line.partition(b"\t")
            fields = metadata.split()
            if len(fields) < 5:
                return None
            try:
                old_oid, new_oid = (field.decode("ascii") for field in fields[:2])
            except UnicodeDecodeError:
                return None
            if _OID.fullmatch(old_oid) is None or _OID.fullmatch(new_oid) is None:
                return None
            entries.append((old_oid, new_oid))

    if [new_oid for _, new_oid in reversed(entries)] != shown_oids:
        return None

    oids = {tip, *shown_oids}
    # `reflog show` lists each entry's new OID, but not the predecessor before its oldest
    # retained row. Reading the raw log above captures it; rev-parse independently verifies
    # Git can resolve that boundary. A new branch's all-zero predecessor is the one normal
    # case where @{N} has no resolvable object.
    if entries:
        oldest_old = entries[0][0]
        boundary = await git(repo, "rev-parse", "--verify", f"{ref}@{{{len(entries)}}}", check=False, env=env)
        if boundary.returncode == 0:
            boundary_oid = boundary.stdout.strip()
            if _OID.fullmatch(boundary_oid) is None or boundary_oid != oldest_old:
                return None
            if int(boundary_oid, 16) != 0:
                oids.add(boundary_oid)
        elif int(oldest_old, 16) != 0 or f"only has {len(entries)} entries" not in boundary.stderr:
            return None
        if int(oldest_old, 16) != 0:
            oids.add(oldest_old)
        for old_oid, new_oid in entries:
            if int(old_oid, 16) != 0:
                oids.add(old_oid)
            if int(new_oid, 16) != 0:
                oids.add(new_oid)

    candidates = sorted(oids)
    checked = await git(
        repo,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
        check=False,
        input_text="".join(f"{oid}\n" for oid in candidates),
    )
    if checked.returncode != 0:
        return None
    rows = [line.split() for line in checked.stdout.splitlines()]
    if len(rows) != len(candidates) or any(row != [oid, "commit"] for oid, row in zip(candidates, rows, strict=True)):
        return None
    return candidates


def _normalize_remote_url(url: str) -> str:
    """A best-effort key for comparing two remote URLs that may name the same repo.

    Not a full git-URL-equivalence engine (no host aliasing, no fork-network resolution) —
    just enough to match the common spellings of one remote: a trailing `.git`, a trailing
    slash, and an SSH vs HTTPS form of the same host and path.
    """
    url = url.strip().removesuffix("/").removesuffix(".git").removeprefix("git+")
    if url.startswith("git@"):
        host, _, path = url.removeprefix("git@").partition(":")
        url = f"{host}/{path}"
    else:
        url = re.sub(r"^\w+://", "", url)  # strip a URL scheme (https://, ssh://, git://)
        url = re.sub(r"^[^@/]+@", "", url)  # strip a leading user@ (ssh://user@host/...)
    return url.lower()


def remote_urls(pg: pygit2.Repository) -> set[str]:
    """Every remote configured on `pg`, normalized for cross-repo comparison — not just
    `origin`, since a clone may track the same project under a different remote name. Takes an
    already-open repository rather than a path: the caller opens once and reuses the handle,
    rather than this function opening its own independent copy of a path someone else may have
    already opened (or is about to)."""
    return {_normalize_remote_url(remote.url) for remote in pg.remotes if remote.url}


def shares_a_remote(a: pygit2.Repository, b: pygit2.Repository) -> bool:
    """True when `a` and `b` have any remote URL in common — the same project, on any host,
    under any remote name, regardless of how many remotes either side configures. Says nothing
    about which (if either) is a GitHub repo; that is a separate, narrower question."""
    return bool(remote_urls(a) & remote_urls(b))


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    branch: str | None  # None for a detached HEAD
    head: str | None = None  # captured commit for a detached HEAD


async def list_worktrees(repo: Path) -> list[Worktree]:
    """Every worktree of `repo` (main included), parsed from `git worktree list --porcelain`."""
    worktrees: list[Worktree] = []
    path: Path | None = None
    branch: str | None = None
    head: str | None = None
    for line in [*(await git_out(repo, "worktree", "list", "--porcelain")).splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("HEAD "):
            head = line.removeprefix("HEAD ")
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif line == "" and path is not None:
            worktrees.append(Worktree(path=path, branch=branch, head=head if branch is None else None))
            path, branch, head = None, None, None
    return worktrees


def main_ref(pg: pygit2.Repository) -> str:
    """The upstream default branch ref (e.g. `origin/devel`).

    A scratch clone nobody ever ran `git remote set-head` on leaves `refs/remotes/origin/HEAD`
    unset — a real condition, not just a theoretical one — so a missing or non-symbolic
    reference is `GitError`, not an assertion failure.
    """
    ref = pg.references.get("refs/remotes/origin/HEAD")
    if ref is None or ref.type != pygit2.enums.ReferenceType.SYMBOLIC or not isinstance(ref.target, str):
        raise GitError("cannot determine the default branch (origin/HEAD is unset)")
    return ref.target.removeprefix("refs/remotes/")


def default_branch_name(pg: pygit2.Repository) -> str:
    """The default branch's short name (e.g. `devel`), from `origin/HEAD`."""
    ref = main_ref(pg)  # e.g. "origin/devel"
    _, _, branch = ref.partition("/")
    return branch or ref


async def main_worktree(repo: Path) -> Path:
    """The main (non-linked) worktree — its `.git` is the common dir's parent."""
    return Path(await git_out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent


async def patches_landed_in_main(repo: Path, head: str, main: str) -> bool:
    """True when every commit `head` adds since the merge base has an equivalent on `main`.

    Complements [`content_in_main`], which asks whether merging `head` would change `main`
    *today* — a question that decays as `main` advances. A branch squash- or rebase-merged
    long ago can conflict with a `main` that has since moved thousands of commits past the
    merge base, and then reads as unlanded even though its work is in. This asks the
    time-invariant question instead: did these patches land at some point after the merge
    base? `git cherry` answers it by patch id, the same equivalence `git rebase` uses to drop
    commits it has already applied.

    Weaker than `content_in_main` on purpose: it establishes that the work landed, not that
    it survives in `main`'s tree, so a later revert still reads as landed. For GC that is the
    right bar — the branch contributes no patch `main` has not already seen — but it is only
    ever an additional reason to prune, never a reason to keep.
    """
    outcome = await git(repo, "cherry", main, head, check=False)
    if outcome.returncode != 0:
        return False
    lines = [line for line in outcome.stdout.splitlines() if line.strip()]
    # `-` marks a commit with an upstream equivalent, `+` one without. An empty listing means
    # nothing to land, which `content_in_main` already covers; require real evidence here.
    return bool(lines) and all(line.startswith("-") for line in lines)


def content_in_main(pg: pygit2.Repository, head: pygit2.Oid, main: str) -> bool:
    """True when commit `head` adds nothing not already in `main`.

    Covers a direct merge (`head` is an ancestor of main), an empty branch (`head` is the
    merge base), and a squash/rebase-merge (merging `head` into main yields main's exact
    tree, so `head` contributed no net change).
    """
    main_commit = pg.revparse_single(main).peel(pygit2.Commit)
    main_oid = main_commit.id
    if pg.descendant_of(main_oid, head):  # main descends from HEAD, i.e. HEAD is in main
        return True
    base = pg.merge_base(main_oid, head)
    if base is None:
        return False
    if base == head:
        return True
    try:
        index = pg.merge_commits(main_oid, head)
    except KeyError:
        # A partial clone (`blob:none`) may not have fetched every blob this three-way tree
        # merge touches, and libgit2 has no lazy-fetch fallback for that — unlike the `git`
        # CLI (which `patches_landed_in_main` shells out to), it never fetches missing objects
        # from the promisor remote on demand. Treat as "can't prove it's already in main"
        # rather than crash; `patches_landed_in_main` can still recognize the squash-merge.
        logger.warning("content_in_main: missing objects to merge %s into %s; treating as unmerged", head, main)
        return False
    if index.conflicts is not None:  # real divergence
        return False
    return index.write_tree(pg) == main_commit.tree.id

"""Read the file tree at a branch tip out of a bare git mirror.

Only the tip is ever read. History is never indexed and never chunked, so no amount of
staleness or partial failure can surface content from an old commit — the searchable set is
whatever `list_tip` returned on the last successful sync.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pygit2


@dataclass(frozen=True, slots=True)
class TipEntry:
    """One blob reachable at the branch tip, at the path it occupies there."""

    path: str
    blob_sha: str


def _callbacks(username: str | None, password: str | None) -> pygit2.RemoteCallbacks | None:
    """Credentials for an HTTP(S) remote, or None for an anonymous/local one."""
    if username is None or password is None:
        return None
    return pygit2.RemoteCallbacks(credentials=pygit2.UserPass(username, password))


def open_mirror(path: Path, url: str, *, username: str | None = None, password: str | None = None) -> pygit2.Repository:
    """Open the bare mirror at `path`, cloning it from `url` if it isn't there yet."""
    if (path / "HEAD").exists():
        return pygit2.Repository(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    return pygit2.clone_repository(url, str(path), bare=True, callbacks=_callbacks(username, password))


def fetch_branch(
    repo: pygit2.Repository, branch: str, *, username: str | None = None, password: str | None = None
) -> str:
    """Fetch `branch` into the mirror and return the commit id it now points at.

    The refspec is forced (`+`) because the mirror tracks whatever the remote branch is now,
    including after a history rewrite. That is safe here precisely because nothing downstream
    reads history: a rewritten tip is just a different set of blobs to index.
    """
    repo.remotes["origin"].fetch(
        refspecs=[f"+refs/heads/{branch}:refs/heads/{branch}"], callbacks=_callbacks(username, password)
    )
    return str(repo.references[f"refs/heads/{branch}"].peel(pygit2.Commit).id)


def _walk(repo: pygit2.Repository, tree: pygit2.Tree, prefix: str) -> Iterator[TipEntry]:
    for entry in tree:
        path = f"{prefix}{entry.name}"
        match entry.type_str:
            case "blob":
                yield TipEntry(path=path, blob_sha=str(entry.id))
            case "tree":
                yield from _walk(repo, repo[entry.id].peel(pygit2.Tree), f"{path}/")
            case "commit":
                # A gitlink (submodule): its content isn't in this repository at all.
                continue


def list_tip(repo: pygit2.Repository, commit_sha: str) -> list[TipEntry]:
    """Every blob reachable from `commit_sha`, i.e. `git ls-tree -r`."""
    commit = repo[commit_sha].peel(pygit2.Commit)
    return list(_walk(repo, commit.tree, ""))


def read_blob(repo: pygit2.Repository, blob_sha: str) -> bytes:
    return repo[blob_sha].peel(pygit2.Blob).data

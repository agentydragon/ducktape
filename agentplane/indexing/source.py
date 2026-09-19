"""One branch of one Git remote as validated, source-independent file snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pathspec
import pygit2
from pydantic import BaseModel, ConfigDict, Field
from pygit2.enums import FetchPrune, FileMode


@dataclass(frozen=True)
class Snapshot:
    revision: str
    tree_id: str
    repository_url: str
    files: Mapping[str, bytes]


class SnapshotLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    file_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    entries: int = Field(default=100_000, gt=0)


def read_tree(
    repository: pygit2.Repository, tree: pygit2.Tree, *, ignore: pathspec.GitIgnoreSpec, limits: SnapshotLimits
) -> Mapping[str, bytes]:
    """Regular files of a tree; an ignored directory is never descended, as in git itself."""
    files: dict[str, bytes] = {}
    total = 0
    pending: list[tuple[str, pygit2.Tree]] = [("", tree)]
    while pending:
        prefix, current = pending.pop()
        for entry in current:
            path = f"{prefix}{entry.name}"
            if entry.filemode == FileMode.TREE:
                if not ignore.match_file(f"{path}/"):
                    subtree = repository[entry.id]
                    assert isinstance(subtree, pygit2.Tree)
                    pending.append((f"{path}/", subtree))
                continue
            # Symlinks are not documents and must never be followed; a submodule entry names a
            # commit this repository does not hold.
            if entry.filemode in (FileMode.LINK, FileMode.COMMIT) or ignore.match_file(path):
                continue
            if len(files) >= limits.entries:
                raise ValueError("Snapshot exceeds entry limit")
            blob = repository[entry.id]
            assert isinstance(blob, pygit2.Blob)
            if blob.size > limits.file_bytes:
                raise ValueError(f"File exceeds byte limit: {path!r}")
            total += blob.size
            if total > limits.total_bytes:
                raise ValueError("Snapshot exceeds total byte limit")
            files[path] = blob.data
    return MappingProxyType(files)


class GitSource:
    """A bare clone of one branch, fetched incrementally on every poll.

    Reads go straight to the tree objects, so the clone never has a working tree. libgit2 has
    no repack, so fetches accumulate pack files until the pod's emptyDir is recreated.
    """

    def __init__(
        self,
        *,
        url: str,
        branch: str,
        path: Path,
        credentials: pygit2.UserPass | None,
        ignore: pathspec.GitIgnoreSpec,
        limits: SnapshotLimits,
    ) -> None:
        self._url = url
        self._branch = branch
        self._path = path
        self._callbacks = pygit2.RemoteCallbacks(credentials=credentials)
        self._ignore = ignore
        self._limits = limits
        self._refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"

    async def snapshot(
        self, *, current_tree_id: str | None, current_revision: str | None, current_repository_url: str | None
    ) -> Snapshot | None:
        return await asyncio.to_thread(self._snapshot, current_tree_id, current_revision, current_repository_url)

    def _repository(self) -> pygit2.Repository:
        if (self._path / "HEAD").exists():
            repository = pygit2.Repository(str(self._path))
            if repository.remotes["origin"].url != self._url:
                repository.remotes.set_url("origin", self._url)
            return repository
        repository = pygit2.init_repository(str(self._path), bare=True)
        repository.remotes.create("origin", self._url, self._refspec)
        return repository

    def _snapshot(
        self, current_tree_id: str | None, current_revision: str | None, current_repository_url: str | None
    ) -> Snapshot | None:
        repository = self._repository()
        repository.remotes["origin"].fetch(refspecs=[self._refspec], callbacks=self._callbacks, prune=FetchPrune.PRUNE)
        commit = repository.lookup_reference(f"refs/remotes/origin/{self._branch}").peel(pygit2.Commit)
        revision, tree_id = str(commit.id), str(commit.tree_id)
        if (tree_id, revision, self._url) == (current_tree_id, current_revision, current_repository_url):
            return None
        return Snapshot(
            revision=revision,
            tree_id=tree_id,
            repository_url=self._url,
            files=read_tree(repository, commit.tree, ignore=self._ignore, limits=self._limits),
        )

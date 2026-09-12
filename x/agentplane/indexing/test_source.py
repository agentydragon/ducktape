from pathlib import Path

import pathspec
import pygit2
import pytest
import pytest_bazel

from x.agentplane.indexing.conftest import Upstream
from x.agentplane.indexing.source import GitSource, SnapshotLimits

NO_IGNORE = pathspec.GitIgnoreSpec.from_lines([])
SUBMODULE_COMMIT = "1" * 40


def source(
    upstream: Upstream, path: Path, *, ignore: pathspec.GitIgnoreSpec = NO_IGNORE, limits: SnapshotLimits | None = None
) -> GitSource:
    return GitSource(
        url=upstream.url, branch="main", path=path, credentials=None, ignore=ignore, limits=limits or SnapshotLimits()
    )


async def test_snapshot_reads_regular_files_only(upstream: Upstream, tmp_path: Path) -> None:
    revision = upstream.commit(
        {"src/example.py": b"print('test')\n", "empty.txt": b"", "bin/tool": b"#!/bin/sh\n"},
        executable=frozenset({"bin/tool"}),
        symlinks={"link": "empty.txt"},
        submodules={"vendor/dependency": SUBMODULE_COMMIT},
    )
    snapshot = await source(upstream, tmp_path / "clone").snapshot(
        current_tree_id=None, current_revision=None, current_repository_url=None
    )
    assert snapshot is not None
    assert snapshot.files == {"src/example.py": b"print('test')\n", "empty.txt": b"", "bin/tool": b"#!/bin/sh\n"}
    assert snapshot.revision == revision
    assert snapshot.tree_id == upstream.tree_id(revision)
    assert snapshot.repository_url == upstream.url
    assert not (tmp_path / "clone" / "src").exists(), "the clone is bare"


async def test_skip_unchanged_and_follow_new_commits(upstream: Upstream, tmp_path: Path) -> None:
    upstream.commit({"doc.txt": b"one\n", "gone.txt": b"bye\n"})
    reader = source(upstream, tmp_path / "clone")
    first = await reader.snapshot(current_tree_id=None, current_revision=None, current_repository_url=None)
    assert first is not None
    current = {
        "current_tree_id": first.tree_id,
        "current_revision": first.revision,
        "current_repository_url": first.repository_url,
    }
    assert await reader.snapshot(**current) is None
    revision = upstream.commit({"doc.txt": b"two\n", "gone.txt": None})
    second = await reader.snapshot(**current)
    assert second is not None
    assert second.revision == revision
    assert second.files == {"doc.txt": b"two\n"}


async def test_ignore_patterns_prune_directories_and_match_globs(upstream: Upstream, tmp_path: Path) -> None:
    upstream.commit(
        {
            "props/specimens/x/y.txt": b"copy\n",
            "notes/blob.gz": b"\x1f\x8b",
            "keep.txt": b"keep\n",
            "props/a.md": b"a\n",
        }
    )
    snapshot = await source(
        upstream, tmp_path / "clone", ignore=pathspec.GitIgnoreSpec.from_lines(["props/specimens/", "*.gz"])
    ).snapshot(current_tree_id=None, current_revision=None, current_repository_url=None)
    assert snapshot is not None
    assert snapshot.files == {"keep.txt": b"keep\n", "props/a.md": b"a\n"}


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (SnapshotLimits(file_bytes=3), "File exceeds byte limit"),
        (SnapshotLimits(total_bytes=5), "total byte limit"),
        (SnapshotLimits(entries=1), "entry limit"),
    ],
)
async def test_limits_reject_the_whole_snapshot(
    upstream: Upstream, tmp_path: Path, limits: SnapshotLimits, message: str
) -> None:
    upstream.commit({"a.txt": b"aaaa", "b.txt": b"bb"})
    with pytest.raises(ValueError, match=message):
        await source(upstream, tmp_path / "clone", limits=limits).snapshot(
            current_tree_id=None, current_revision=None, current_repository_url=None
        )


async def test_relocated_remote_refetches_from_the_new_url(upstream: Upstream, tmp_path: Path) -> None:
    upstream.commit({"doc.txt": b"old home\n"})
    first = await source(upstream, tmp_path / "clone").snapshot(
        current_tree_id=None, current_revision=None, current_repository_url=None
    )
    assert first is not None
    moved = Upstream(pygit2.init_repository(str(tmp_path / "moved"), initial_head="main"))
    moved.commit({"doc.txt": b"new home\n"})
    second = await source(moved, tmp_path / "clone").snapshot(
        current_tree_id=first.tree_id, current_revision=first.revision, current_repository_url=first.repository_url
    )
    assert second is not None
    assert second.repository_url == moved.url
    assert second.files == {"doc.txt": b"new home\n"}


if __name__ == "__main__":
    pytest_bazel.main()

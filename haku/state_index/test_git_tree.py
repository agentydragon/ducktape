"""The mirror must follow the branch tip, including across a history rewrite."""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from haku.state_index.git_tree import fetch_branch, list_tip, open_mirror, read_blob, remote_tip

_AUTHOR = pygit2.Signature("Test", "test@example.com")


def _commit(repo: pygit2.Repository, files: dict[str, str], *, parents: list[str] | None = None) -> str:
    index = pygit2.Index()
    for path, content in files.items():
        blob = repo.create_blob(content.encode())
        index.add(pygit2.IndexEntry(path, blob, pygit2.enums.FileMode.BLOB))
    if parents is None:
        parents = [] if repo.head_is_unborn else [str(repo.references["refs/heads/main"].target)]
    # Created detached and force-moved onto the branch, so a test can rewrite history (an
    # empty parent list under an existing tip) the way a squash-merge does.
    commit = repo.create_commit(None, _AUTHOR, _AUTHOR, "commit", index.write_tree(repo), parents)
    repo.references.create("refs/heads/main", commit, force=True)
    return str(commit)


@pytest.fixture
def source(tmp_path: Path) -> pygit2.Repository:
    return pygit2.init_repository(str(tmp_path / "source.git"), bare=True, initial_head="main")


def test_lists_every_blob_with_its_full_path(source: pygit2.Repository) -> None:
    commit = _commit(source, {"a.md": "alpha", "dir/b.md": "beta", "dir/deep/c.md": "gamma"})
    assert {entry.path for entry in list_tip(source, commit)} == {"a.md", "dir/b.md", "dir/deep/c.md"}


def test_blob_sha_reads_back_the_content(source: pygit2.Repository) -> None:
    commit = _commit(source, {"a.md": "alpha"})
    (entry,) = list_tip(source, commit)
    assert read_blob(source, entry.blob_sha) == b"alpha"


def test_identical_content_at_two_paths_shares_one_blob(source: pygit2.Repository) -> None:
    commit = _commit(source, {"a.md": "same", "b.md": "same"})
    assert len({entry.blob_sha for entry in list_tip(source, commit)}) == 1


def test_fetch_follows_the_branch_forward(tmp_path: Path, source: pygit2.Repository) -> None:
    _commit(source, {"a.md": "alpha"})
    mirror = open_mirror(tmp_path / "mirror.git", f"file://{source.path}")
    second = _commit(source, {"a.md": "alpha", "b.md": "beta"})

    assert fetch_branch(mirror, "main") == second
    assert {entry.path for entry in list_tip(mirror, second)} == {"a.md", "b.md"}


def test_fetch_follows_a_rewritten_history(tmp_path: Path, source: pygit2.Repository) -> None:
    """A squash/rebase on the remote must not wedge the mirror: nothing here reads history."""
    _commit(source, {"a.md": "alpha", "gone.md": "removed later"})
    mirror = open_mirror(tmp_path / "mirror.git", f"file://{source.path}")
    fetch_branch(mirror, "main")

    rewritten = _commit(source, {"a.md": "alpha"}, parents=[])

    assert fetch_branch(mirror, "main") == rewritten
    assert {entry.path for entry in list_tip(mirror, rewritten)} == {"a.md"}


def test_remote_tip_reads_the_branch_without_fetching_it(tmp_path: Path, source: pygit2.Repository) -> None:
    """The cheap poll: it must answer "has it moved?" without paying for the objects."""
    _commit(source, {"a.md": "alpha"})
    mirror = open_mirror(tmp_path / "mirror.git", f"file://{source.path}")
    second = _commit(source, {"a.md": "alpha", "b.md": "beta"})

    assert remote_tip(mirror, "main") == second
    assert second not in mirror


def test_remote_tip_is_none_for_a_branch_the_remote_does_not_have(tmp_path: Path, source: pygit2.Repository) -> None:
    _commit(source, {"a.md": "alpha"})
    mirror = open_mirror(tmp_path / "mirror.git", f"file://{source.path}")

    assert remote_tip(mirror, "trunk") is None


if __name__ == "__main__":
    pytest_bazel.main()

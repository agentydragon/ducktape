"""Tests for filename_conventions."""

from __future__ import annotations

import pygit2
import pytest

from devinfra.precommit.conftest import commit, stage
from devinfra.precommit.filename_conventions import check_filename_conventions
from devinfra.precommit.git_hook import _staged_deltas


def _check(repo: pygit2.Repository) -> list[str]:
    head_tree, deltas = _staged_deltas(repo)
    return check_filename_conventions(deltas, head_tree)


def test_no_violations_with_underscores(repo: pygit2.Repository) -> None:
    stage(repo, "foo_bar.py")
    assert _check(repo) == []


@pytest.mark.parametrize("name", ["foo-bar.py", "foo-bar.md"])
def test_new_py_or_md_file_with_dash(repo: pygit2.Repository, name: str) -> None:
    stage(repo, name)
    violations = _check(repo)
    assert len(violations) == 1
    assert name in violations[0]
    assert "underscore" in violations[0]


def test_existing_file_modification_not_flagged(repo: pygit2.Repository) -> None:
    stage(repo, "foo-bar.py")
    commit(repo)

    stage(repo, "foo-bar.py", "# modified")

    assert _check(repo) == []


def test_new_directory_with_dash(repo: pygit2.Repository) -> None:
    stage(repo, "existing.py")
    commit(repo)

    stage(repo, "my-dir/foo.py")

    violations = _check(repo)
    assert len(violations) == 1
    assert "my-dir" in violations[0]
    assert "directory" in violations[0]


def test_existing_directory_with_dash_not_flagged(repo: pygit2.Repository) -> None:
    stage(repo, "my-dir/existing.py")
    commit(repo)

    stage(repo, "my-dir/new_file.py")

    assert _check(repo) == []


def test_other_extensions_not_checked(repo: pygit2.Repository) -> None:
    stage(repo, "foo-bar.txt", "test")
    assert _check(repo) == []


def test_no_staged_changes(repo: pygit2.Repository) -> None:
    assert _check(repo) == []


def test_both_filename_and_directory_violations(repo: pygit2.Repository) -> None:
    stage(repo, "bad-dir/bad-name.py")

    violations = _check(repo)
    assert len(violations) == 2
    filenames = [v for v in violations if "filename" in v]
    dirs = [v for v in violations if "directory" in v]
    assert len(filenames) == 1
    assert len(dirs) == 1

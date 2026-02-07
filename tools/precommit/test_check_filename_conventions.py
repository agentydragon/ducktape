"""Tests for check_filename_conventions."""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from tools.precommit.check_filename_conventions import check_filename_conventions


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    repo = pygit2.init_repository(str(tmp_path))
    repo.config["user.name"] = "Test"
    repo.config["user.email"] = "test@test.com"
    return repo


def _commit(repo: pygit2.Repository) -> None:
    sig = pygit2.Signature("Test", "test@test.com")
    tree = repo.index.write_tree()
    parents = [repo.head.target] if not repo.head_is_unborn else []
    repo.create_commit("HEAD", sig, sig, "commit", tree, parents)


def test_no_violations_with_underscores(repo: pygit2.Repository, tmp_path: Path) -> None:
    (tmp_path / "foo_bar.py").write_text("# test")
    repo.index.add("foo_bar.py")
    repo.index.write()
    assert check_filename_conventions(repo) == []


def test_py_file_with_dash(repo: pygit2.Repository, tmp_path: Path) -> None:
    (tmp_path / "foo-bar.py").write_text("# test")
    repo.index.add("foo-bar.py")
    repo.index.write()
    violations = check_filename_conventions(repo)
    assert len(violations) == 1
    assert "foo-bar.py" in violations[0]
    assert "underscore" in violations[0]


def test_md_file_with_dash(repo: pygit2.Repository, tmp_path: Path) -> None:
    (tmp_path / "foo-bar.md").write_text("# test")
    repo.index.add("foo-bar.md")
    repo.index.write()
    violations = check_filename_conventions(repo)
    assert len(violations) == 1
    assert "foo-bar.md" in violations[0]


def test_existing_file_modification_not_flagged(repo: pygit2.Repository, tmp_path: Path) -> None:
    (tmp_path / "foo-bar.py").write_text("# test")
    repo.index.add("foo-bar.py")
    repo.index.write()
    _commit(repo)

    (tmp_path / "foo-bar.py").write_text("# modified")
    repo.index.add("foo-bar.py")
    repo.index.write()

    assert check_filename_conventions(repo) == []


def test_new_directory_with_dash(repo: pygit2.Repository, tmp_path: Path) -> None:
    (tmp_path / "existing.py").write_text("# test")
    repo.index.add("existing.py")
    repo.index.write()
    _commit(repo)

    new_dir = tmp_path / "my-dir"
    new_dir.mkdir()
    (new_dir / "foo.py").write_text("# test")
    repo.index.add("my-dir/foo.py")
    repo.index.write()

    violations = check_filename_conventions(repo)
    assert len(violations) == 1
    assert "my-dir" in violations[0]
    assert "directory" in violations[0]


def test_existing_directory_with_dash_not_flagged(repo: pygit2.Repository, tmp_path: Path) -> None:
    dash_dir = tmp_path / "my-dir"
    dash_dir.mkdir()
    (dash_dir / "existing.py").write_text("# test")
    repo.index.add("my-dir/existing.py")
    repo.index.write()
    _commit(repo)

    (dash_dir / "new_file.py").write_text("# test")
    repo.index.add("my-dir/new_file.py")
    repo.index.write()

    assert check_filename_conventions(repo) == []


def test_other_extensions_not_checked(repo: pygit2.Repository, tmp_path: Path) -> None:
    (tmp_path / "foo-bar.txt").write_text("test")
    repo.index.add("foo-bar.txt")
    repo.index.write()
    assert check_filename_conventions(repo) == []


def test_no_staged_changes(repo: pygit2.Repository) -> None:
    assert check_filename_conventions(repo) == []


def test_both_filename_and_directory_violations(repo: pygit2.Repository, tmp_path: Path) -> None:
    new_dir = tmp_path / "bad-dir"
    new_dir.mkdir()
    (new_dir / "bad-name.py").write_text("# test")
    repo.index.add("bad-dir/bad-name.py")
    repo.index.write()

    violations = check_filename_conventions(repo)
    assert len(violations) == 2
    filenames = [v for v in violations if "filename" in v]
    dirs = [v for v in violations if "directory" in v]
    assert len(filenames) == 1
    assert len(dirs) == 1


if __name__ == "__main__":
    pytest_bazel.main()

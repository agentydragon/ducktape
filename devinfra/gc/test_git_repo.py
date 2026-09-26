from collections.abc import Callable
from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from devinfra.gc import git_repo
from devinfra.gc.conftest import GitRepo


@pytest.fixture
def fresh_repo(tmp_path: Path) -> Callable[[str], GitRepo]:
    """Factory for an independent repo with no remotes — unlike `.worktree()`, which shares
    the caller's own `.git` config (and hence its remotes), or `.clone()`, which always gets
    an `origin`."""

    def make(name: str) -> GitRepo:
        path = tmp_path / name
        pygit2.init_repository(str(path), initial_head="main")
        made = GitRepo(path)
        made.commit("base", "0\n", "init")
        return made

    return make


def test_shares_a_remote_matches_ssh_and_https_forms_of_the_same_repo(
    repo: GitRepo, fresh_repo: Callable[[str], GitRepo]
) -> None:
    repo.set_origin("git@github.com:acme/widgets.git")
    other = fresh_repo("other")
    other.set_origin("https://github.com/acme/widgets")

    assert git_repo.shares_a_remote(repo.path, other.path)


def test_shares_a_remote_false_for_different_projects(repo: GitRepo, fresh_repo: Callable[[str], GitRepo]) -> None:
    repo.set_origin("https://github.com/acme/widgets")
    other = fresh_repo("other")
    other.set_origin("https://github.com/acme/gadgets")

    assert not git_repo.shares_a_remote(repo.path, other.path)


def test_shares_a_remote_checks_every_remote_not_just_origin(
    repo: GitRepo, fresh_repo: Callable[[str], GitRepo]
) -> None:
    """A fork's `origin` differs from the project's canonical remote, so matching must look at
    every configured remote — not assume both sides call the shared one `origin`."""
    repo.set_origin("https://github.com/acme/widgets")
    fork = fresh_repo("fork")
    fork.set_origin("https://github.com/someone/widgets-fork")
    fork.run("remote", "add", "upstream", "https://github.com/acme/widgets")

    assert git_repo.shares_a_remote(repo.path, fork.path)


def test_shares_a_remote_false_with_no_remotes(repo: GitRepo, fresh_repo: Callable[[str], GitRepo]) -> None:
    other = fresh_repo("other")
    assert not git_repo.shares_a_remote(repo.path, other.path)


def test_remote_urls_empty_for_a_path_that_no_longer_opens_as_a_repo(tmp_path: Path) -> None:
    """A caller may have already confirmed a candidate path opens (`_find_repo_root`'s own
    pygit2 check) moments before `remote_urls` opens it again independently — a real TOCTOU gap
    for an untrusted, possibly-racy path like a scratch clone under `/tmp`. Losing the race
    (the path stops being a repo in between) must surface as no remotes, not a crash."""
    gone = tmp_path / "not-a-repo"
    gone.mkdir()

    assert git_repo.remote_urls(gone) == set()


def test_main_ref_raises_git_error_when_origin_head_is_unset(repo: GitRepo) -> None:
    """`git symbolic-ref` exits non-zero (not just empty output) when `origin/HEAD` was never
    set — a real condition for a scratch clone nobody ran `git remote set-head` on. This must
    surface as `GitError`, the one exception type callers actually catch — not a bare
    `CalledProcessError` that slips past them."""
    with pytest.raises(git_repo.GitError):
        git_repo.main_ref(repo.path)


if __name__ == "__main__":
    pytest_bazel.main()

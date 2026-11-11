import os
import signal
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

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

    assert git_repo.shares_a_remote(repo.pg(), other.pg())


def test_shares_a_remote_false_for_different_projects(repo: GitRepo, fresh_repo: Callable[[str], GitRepo]) -> None:
    repo.set_origin("https://github.com/acme/widgets")
    other = fresh_repo("other")
    other.set_origin("https://github.com/acme/gadgets")

    assert not git_repo.shares_a_remote(repo.pg(), other.pg())


def test_shares_a_remote_checks_every_remote_not_just_origin(
    repo: GitRepo, fresh_repo: Callable[[str], GitRepo]
) -> None:
    """A fork's `origin` differs from the project's canonical remote, so matching must look at
    every configured remote — not assume both sides call the shared one `origin`."""
    repo.set_origin("https://github.com/acme/widgets")
    fork = fresh_repo("fork")
    fork.set_origin("https://github.com/someone/widgets-fork")
    fork.run("remote", "add", "upstream", "https://github.com/acme/widgets")

    assert git_repo.shares_a_remote(repo.pg(), fork.pg())


def test_shares_a_remote_false_with_no_remotes(repo: GitRepo, fresh_repo: Callable[[str], GitRepo]) -> None:
    other = fresh_repo("other")
    assert not git_repo.shares_a_remote(repo.pg(), other.pg())


def test_open_repo_raises_git_error_for_a_path_that_is_not_a_repo(tmp_path: Path) -> None:
    """`pygit2.Repository`'s own `GitError` is a plain `Exception`, not this module's
    `GitError` — `open_repo` must normalize it so every caller can catch one exception type."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    with pytest.raises(git_repo.GitError):
        git_repo.open_repo(not_a_repo)


def test_main_ref_raises_git_error_when_origin_head_is_unset(repo: GitRepo) -> None:
    """`origin/HEAD` unset is a real condition for a scratch clone nobody ran
    `git remote set-head` on, not just a theoretical one — it must surface as `GitError`."""
    with pytest.raises(git_repo.GitError):
        git_repo.main_ref(repo.pg())


def test_interrupt_kills_read_but_allows_admitted_mutation_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "release"
    os.mkfifo(fifo)
    fake_git = tmp_path / "git"
    fake_git.write_text('#!/bin/sh\nIFS= read -r value < "$WORKSPACE_GC_TEST_FIFO"\n')
    fake_git.chmod(0o755)
    monkeypatch.setenv("WORKSPACE_GC_TEST_FIFO", str(fifo))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    started = threading.Barrier(3)

    class MonitoredPopen(subprocess.Popen[str]):
        def communicate(self, *args: Any, **kwargs: Any) -> tuple[str, str]:
            started.wait(timeout=10)
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(git_repo.subprocess, "Popen", MonitoredPopen)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            read = pool.submit(git_repo.git, tmp_path, "read", check=False)
            mutation = pool.submit(git_repo.git, tmp_path, "mutate", check=False, interruptible=False)
            try:
                started.wait(timeout=10)
                git_repo.interrupt_running_git()
                assert read.result(timeout=10).returncode == -signal.SIGKILL
                os.write(fd, b"release\n")
                assert mutation.result(timeout=10).returncode == 0
            finally:
                os.write(fd, b"release\nrelease\n")
    finally:
        os.close(fd)


if __name__ == "__main__":
    pytest_bazel.main()

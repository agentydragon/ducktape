import asyncio
import os
from collections.abc import Callable
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


@pytest.mark.parametrize("delay_creation", [False, True])
async def test_cancel_reaps_read_but_waits_for_noninterruptible_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delay_creation: bool
) -> None:
    fifo = tmp_path / "release"
    os.mkfifo(fifo)
    fake_git = tmp_path / "git"
    fake_git.write_text('#!/bin/sh\nIFS= read -r value < "$WORKSPACE_GC_TEST_FIFO"\n')
    fake_git.chmod(0o755)
    monkeypatch.setenv("WORKSPACE_GC_TEST_FIFO", str(fifo))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    create = asyncio.create_subprocess_exec
    creation_allowed = asyncio.Event()
    if not delay_creation:
        creation_allowed.set()
    events = {name: asyncio.Event() for name in ("read", "mutate")}
    processes: dict[str, asyncio.subprocess.Process] = {}

    async def monitored(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await create(*args, **kwargs)
        name = args[-1]
        processes[name] = process
        events[name].set()
        await creation_allowed.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", monitored)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    read = asyncio.create_task(git_repo.git(tmp_path, "read", check=False))
    mutation = asyncio.create_task(git_repo.git(tmp_path, "mutate", check=False, interruptible=False))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in events.values())), 10)
        read.cancel()
        mutation.cancel()
        await asyncio.sleep(0)
        read.cancel()
        mutation.cancel()
        creation_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, 10)
        with pytest.raises(ProcessLookupError):
            os.kill(processes["read"].pid, 0)
        assert not mutation.done()
        os.write(fd, b"release\n")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(mutation, 10)
        with pytest.raises(ProcessLookupError):
            os.kill(processes["mutate"].pid, 0)
    finally:
        creation_allowed.set()
        os.write(fd, b"release\nrelease\n")
        await asyncio.gather(read, mutation, return_exceptions=True)
        os.close(fd)


if __name__ == "__main__":
    pytest_bazel.main()

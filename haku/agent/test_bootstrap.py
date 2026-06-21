"""Tests for the startup bootstrap (netrc write + pygit2 clone/refresh)."""

from pathlib import Path

import pygit2
import pytest_bazel

from haku.agent.bootstrap import clone_or_refresh, write_netrc


def test_write_netrc_format_and_mode(tmp_path: Path) -> None:
    netrc = tmp_path / ".netrc"
    write_netrc(host="git.example", username="u", password="p", path=netrc)
    assert netrc.read_text() == "machine git.example\n  login u\n  password p\n"
    assert (netrc.stat().st_mode & 0o777) == 0o600


def test_clone_then_refresh(tmp_path: Path) -> None:
    src = tmp_path / "src"
    repo = pygit2.init_repository(str(src), initial_head="main")
    (src / "f.txt").write_text("hello")
    repo.index.add("f.txt")
    repo.index.write()
    sig = pygit2.Signature("t", "t@example")
    repo.create_commit("refs/heads/main", sig, sig, "init", repo.index.write_tree(), [])

    dest = tmp_path / "dest"
    callbacks = pygit2.RemoteCallbacks()
    clone_or_refresh(str(src), dest, callbacks=callbacks)
    assert (dest / "f.txt").read_text() == "hello"

    # Second call refreshes the existing clone (fetch + hard-reset path), still clean.
    clone_or_refresh(str(src), dest, callbacks=callbacks)
    assert (dest / "f.txt").read_text() == "hello"


if __name__ == "__main__":
    pytest_bazel.main()

import os
import shlex
import subprocess

import pygit2
import pytest
import pytest_bazel
from python.runfiles import Runfiles

from x.wt.testing.git_helpers import add_and_commit


@pytest.fixture(params=["copy", "rsync"])
def real_config(request, real_temp_repo, daemon_config_factory):
    return daemon_config_factory(real_temp_repo).integration(github_repo=None, cow_method=request.param)


@pytest.fixture
def copy_cli(real_config, wt_cli, tmp_path):
    if real_config.cow_method == "rsync":
        closure = Runfiles.CreateOrRaise().root() / os.environ["RSYNC_CLOSURE"]
        subprocess.run(["tar", "-xf", closure, "-C", tmp_path], check=True)
        libdir = tmp_path / "usr/lib/x86_64-linux-gnu"
        argv = [str(libdir / "ld-linux-x86-64.so.2"), "--library-path", str(libdir), str(tmp_path / "usr/bin/rsync")]
        # The daemon resolves rsync on PATH. Run the packaged binary with its own loader and libraries.
        launcher = tmp_path / "rsync"
        launcher.write_text(f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n')
        launcher.chmod(0o755)
        wt_cli.env["PATH"] = os.pathsep.join((str(tmp_path), wt_cli.env["PATH"]))
    return wt_cli


def test_copy_dirty_state_cli(copy_cli, real_config):
    src = "src_wt"
    result = copy_cli.sh_c(src, cwd=real_config.main_repo)
    assert result.returncode == 0, result.stderr
    src_path = real_config.worktrees_dir / src

    (src_path / "untracked.txt").write_text("hello")
    add_and_commit(src_path, {"README.md": "base\n"}, "add readme")
    (src_path / "README.md").write_text("modified\n")
    (src_path / ".worktrees").write_text("worktree metadata")
    (src_path / "nested" / ".git").mkdir(parents=True)
    (src_path / "nested" / "content").write_text("nested content")

    # Distinct name lengths keep rsync's size/mtime check from masking a .git overwrite.
    dst = "dst-with-own-branch"
    result = copy_cli.sh("cp", src, dst, cwd=real_config.main_repo)
    assert result.returncode == 0, result.stderr

    dst_path = real_config.worktrees_dir / dst
    assert (dst_path / "untracked.txt").read_text() == "hello"
    assert (dst_path / "README.md").read_text() == "modified\n"
    assert not (dst_path / ".worktrees").exists()
    assert (dst_path / "nested" / "content").read_text() == "nested content"
    if real_config.cow_method == "rsync":
        assert not (dst_path / "nested" / ".git").exists()
    assert pygit2.Repository(dst_path).head.shorthand == f"{real_config.branch_prefix}{dst}"
    assert pygit2.Repository(src_path).head.shorthand == f"{real_config.branch_prefix}{src}"


if __name__ == "__main__":
    pytest_bazel.main()

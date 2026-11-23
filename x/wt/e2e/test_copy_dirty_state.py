import os
import shlex
import subprocess

import pygit2
import pytest
from python.runfiles import Runfiles

from x.wt.shared.git_utils import git_run
from x.wt.testing.git_helpers import add_and_commit, worktree_exists


@pytest.fixture(params=["reflink", "rsync"])
def real_config(request, real_temp_repo, daemon_config_factory):
    return daemon_config_factory(real_temp_repo).integration(
        github_repo=None, cow_method=request.param, worktrees_dir=str(real_temp_repo / "local" / "[worktrees]")
    )


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


@pytest.mark.parametrize("source", ["linked", "detached", "main", "main-cwd"])
def test_copy_dirty_state(copy_cli, real_config, source):
    src = "src_wt"
    if source in {"linked", "detached"}:
        result = copy_cli.sh_c(src, cwd=real_config.main_repo)
        assert result.returncode == 0, result.stderr
        src_path = real_config.worktrees_dir / src
    else:
        src_path = real_config.main_repo
    # The configured root contains more than registered worktrees. Its neighboring files are ordinary content.
    managed_root = real_config.worktrees_dir
    (managed_root / "unregistered.txt").write_text("managed")
    neighbor = src_path / "local" / "w"
    neighbor.parent.mkdir(exist_ok=True)
    neighbor.parent.chmod(0o750)
    neighbor.write_text("keep")
    neighbor.chmod(0o755)
    (neighbor.parent / "alias").symlink_to("w")

    add_and_commit(src_path, {"changed.txt": "base\n", "deleted.txt": "base\n", "clean.txt": "clean\n"}, "base files")
    if source == "detached":
        git_run(["checkout", "--detach"], cwd=src_path)
    source_repo = pygit2.Repository(src_path)
    src_branch = source_repo.head.shorthand
    source_head = source_repo.head.target
    (src_path / "changed.txt").write_text("staged\n")
    (src_path / "newly-staged.txt").write_text("new content\n")
    (src_path / "deleted.txt").unlink()
    git_run(["add", "--", "changed.txt", "newly-staged.txt", "deleted.txt"], cwd=src_path)
    (src_path / "changed.txt").write_text("staged\nunstaged\n")
    (src_path / "untracked.txt").write_text("hello")
    source_staged_diff = git_run(["diff", "--cached"], cwd=src_path).stdout
    (src_path / ".worktrees").write_text("worktree metadata")
    (src_path / "nested" / ".git").mkdir(parents=True)
    (src_path / "nested" / "content").write_text("nested content")

    # Distinct name lengths keep rsync's size/mtime check from masking a .git overwrite.
    dst = "dst-with-own-branch"
    if source in {"main-cwd", "detached"}:
        result = copy_cli.sh("cp", dst, cwd=neighbor.parent)
    else:
        result = copy_cli.sh("cp", src if source == "linked" else "main", dst, cwd=real_config.main_repo)
    assert result.returncode == 0, result.stderr

    dst_path = real_config.worktrees_dir / dst
    assert pygit2.Repository(dst_path).head.target == source_head
    assert pygit2.Repository(dst_path).head.shorthand == f"{real_config.branch_prefix}{dst}"
    assert pygit2.Repository(src_path).head.shorthand == src_branch
    assert worktree_exists(pygit2.Repository(real_config.main_repo), dst_path)
    assert git_run(["diff", "--cached"], cwd=src_path).stdout == source_staged_diff

    assert not (dst_path / managed_root.relative_to(real_config.main_repo)).exists()
    assert (managed_root / "unregistered.txt").read_text() == "managed"
    copied_neighbor = dst_path / "local" / "w"
    assert copied_neighbor.read_text() == "keep"
    assert copied_neighbor.stat().st_mode == neighbor.stat().st_mode
    assert copied_neighbor.parent.stat().st_mode == neighbor.parent.stat().st_mode
    assert (dst_path / "local" / "alias").readlink() == (neighbor.parent / "alias").readlink()
    assert (dst_path / "changed.txt").read_text() == "staged\nunstaged\n"
    assert (dst_path / "clean.txt").read_text() == "clean\n"
    assert (dst_path / "newly-staged.txt").read_text() == "new content\n"
    assert (dst_path / "untracked.txt").read_text() == "hello"
    assert (dst_path / "nested" / "content").read_text() == "nested content"
    assert not (dst_path / ".worktrees").exists()
    if real_config.cow_method == "rsync":
        assert not (dst_path / "nested" / ".git").exists()
    status = git_run(["status", "--porcelain", "--untracked-files=all"], cwd=dst_path).stdout.decode()
    assert set(status.splitlines()) == {
        " M changed.txt",
        " D deleted.txt",
        "?? newly-staged.txt",
        "?? nested/content",
        "?? local/w",
        "?? local/alias",
        "?? untracked.txt",
    }

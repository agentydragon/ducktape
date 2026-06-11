"""Real integration tests for actual CLI."""

import os
from datetime import timedelta
from pathlib import Path

import pygit2
import pytest

from wt.testing.git_helpers import add_and_commit, worktree_exists
from wt.testing.utils import wait_until

pytestmark = [pytest.mark.timeout(10), pytest.mark.xdist_group("wt-daemon-e2e")]


def test_real_program_workflow(pygit2_repo, real_temp_repo, wt_cli):
    # Initial status
    result = wt_cli.status(timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    # Create first worktree
    result = wt_cli.sh_c("feature1", timeout=timedelta(seconds=10.0))
    assert result.returncode == 0
    worktree1_path = real_temp_repo / "worktrees" / "feature1"
    assert worktree1_path.exists()
    assert (worktree1_path / ".git").exists()

    # Create second worktree
    result = wt_cli.sh_c("feature2", timeout=timedelta(seconds=10.0))
    assert result.returncode == 0
    worktree2_path = real_temp_repo / "worktrees" / "feature2"
    assert worktree2_path.exists()

    # Status shows both (allow brief propagation)
    def _both_present() -> bool:
        r = wt_cli.status(timeout=timedelta(seconds=10.0))
        return r.returncode == 0 and ("feature1" in r.stdout) and ("feature2" in r.stdout)

    assert wt_cli.wait_for(_both_present, timeout=timedelta(seconds=5.0))

    # Navigate to feature1
    result = wt_cli.sh("feature1", timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    # Remove feature2
    result = wt_cli.sh("rm", "feature2", "--force", cwd=worktree1_path, timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    def _removed() -> bool:
        return not worktree_exists(pygit2_repo, worktree2_path)

    assert wt_cli.wait_for(_removed, timeout=timedelta(seconds=5.0))

    # Final status
    result = wt_cli.status(timeout=timedelta(seconds=10.0))
    assert result.returncode == 0


def test_real_daemon_startup_and_communication(real_temp_repo, wt_cli):
    # Start daemon
    result = wt_cli.status(timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    daemon_dir = Path(wt_cli.env["WT_DIR"]).resolve()
    assert daemon_dir.exists()
    pid_file = daemon_dir / "daemon.pid"

    ok = wait_until(lambda: pid_file.exists(), timeout_seconds=2.0, interval_seconds=0.05)
    assert ok, "daemon.pid not created in time"
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, 0)
    except OSError:
        pytest.fail(f"Daemon PID {pid} not found")


def test_real_git_operations(real_temp_repo, wt_cli):
    # Create worktree
    result = wt_cli.sh_c("git-test", timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    worktree_path = real_temp_repo / "worktrees" / "git-test"
    assert worktree_path.exists()

    # Perform git operations using pygit2
    add_and_commit(worktree_path, {"test.txt": "Hello from worktree!"}, "Test commit")

    # Verify branch name using pygit2
    wt_repo = pygit2.Repository(worktree_path)
    assert wt_repo.head.shorthand == "test/git-test"

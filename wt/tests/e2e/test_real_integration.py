"""Real integration tests for actual CLI."""

from datetime import timedelta
import os
from pathlib import Path

import pytest

from wt.shared.git_utils import git_run

from ..test_utils import wait_until

pytestmark = [pytest.mark.timeout(10), pytest.mark.xdist_group("wt-daemon-e2e")]


def test_real_program_workflow(real_temp_repo, wt_cli):
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
        out = git_run(["worktree", "list"], cwd=real_temp_repo).stdout.decode()
        return str(worktree2_path) not in out

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

    # Perform git operations
    (worktree_path / "test.txt").write_text("Hello from worktree!")
    git_run(["add", "test.txt"], cwd=worktree_path)
    git_run(["commit", "-m", "Test commit"], cwd=worktree_path)

    # Verify branch name
    result = git_run(["branch", "--show-current"], cwd=worktree_path, capture_output=True)
    assert "test/git-test" in result.stdout.decode()

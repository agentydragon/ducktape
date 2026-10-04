"""Real integration test - runs actual unmodified CLI against temporary repo."""

import os
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_bazel

from x.wt.testing.utils import wait_until

pytestmark = [pytest.mark.timeout(10), pytest.mark.xdist_group("wt-daemon-e2e")]


def test_real_workflow_with_existing_worktrees(real_env_with_existing_worktrees, real_temp_repo, wtcli):
    """Test workflow starting with existing worktrees - tests real status display."""
    cli = wtcli(real_env_with_existing_worktrees)
    # Step 1: Status should show existing worktrees
    result = cli.status()
    assert result.returncode == 0
    assert "existing-1" in result.stdout
    assert "existing-2" in result.stdout

    # Step 2: Create a new worktree alongside existing ones
    result = cli.sh_c("new-feature")
    assert result.returncode == 0

    # Verify new worktree created
    new_worktree_path = real_temp_repo / "worktrees" / "new-feature"
    assert new_worktree_path.exists()

    # Step 3: Status should now show all three worktrees
    result = cli.status()
    assert result.returncode == 0
    assert "existing-1" in result.stdout
    assert "existing-2" in result.stdout
    assert "new-feature" in result.stdout


def test_real_daemon_startup_and_kill(real_temp_repo, real_env, wt_cli):
    """Test that daemon actually starts and can be killed via CLI command."""
    # Step 1: Initial command should start the daemon
    result = wt_cli.status(timeout=timedelta(seconds=10.0))
    print(f"Initial status (should start daemon) (exit={result.returncode}):\n{result.stdout}\n{result.stderr}")
    assert result.returncode == 0

    # Step 2: Check that daemon files were created

    daemon_dir = Path(real_env["WT_DIR"]).resolve()
    assert daemon_dir.exists(), "Daemon directory not created"

    pid_file = daemon_dir / "daemon.pid"
    # Wait for daemon to start up
    assert wait_until(pid_file.exists, timeout_seconds=2.0, interval_seconds=0.05)

    # Step 3: Verify daemon is actually running
    assert pid_file.exists(), "Daemon PID file not created"
    pid = int(pid_file.read_text().strip())
    # Check if process exists
    try:
        os.kill(pid, 0)  # Signal 0 just checks if process exists
        print(f"✅ Daemon running with PID {pid}")
    except OSError:
        pytest.fail(f"❌ Daemon PID {pid} not found")

    # Step 4: Test kill-daemon command
    result = wt_cli.kill()
    print(f"Kill daemon command (exit={result.returncode}):\n{result.stdout}\n{result.stderr}")
    assert result.returncode == 0

    # Step 5: Verify daemon is no longer running
    wait_until(lambda: not pid_file.exists(), timeout_seconds=2.0, interval_seconds=0.05)

    # Wait for process to actually terminate (SIGKILL may take a moment)
    def process_dead():
        try:
            os.kill(pid, 0)
            return False
        except OSError:
            return True

    if not wait_until(process_dead, timeout_seconds=2.0, interval_seconds=0.05):
        pytest.fail(f"Daemon process {pid} still running after kill command")
    print(f"✅ Daemon process {pid} successfully killed")

    # Step 6: Verify cleanup happened
    # PID file should be removed or contain stale PID
    if pid_file.exists():
        new_pid = int(pid_file.read_text().strip())
        if new_pid == pid:
            pytest.fail("PID file not cleaned up after daemon kill")


if __name__ == "__main__":
    pytest_bazel.main()

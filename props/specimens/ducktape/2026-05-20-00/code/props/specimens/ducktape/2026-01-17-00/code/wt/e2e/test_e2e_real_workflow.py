"""Real integration test - runs actual unmodified CLI against temporary repo.

CRITICAL: Unix Socket Path Length Issue & Solution
==================================================

These E2E tests were failing because the daemon couldn't start due to Unix domain
socket path length limitations. The root cause was pytest temporary directories
generating paths like:

    /private/var/folders/_l/vpt0hb254j1f6nyp0qx84hzw0000gp/T/pytest-of-mpokorny/
    pytest-12/test_real_daemon_startup_and_k0/test_repo/.wt/daemon.sock

This path (160+ characters) exceeds the Unix socket limit (~104 chars), causing:
    OSError: AF_UNIX path too long

SOLUTION: Config.daemon_socket_path now automatically detects long paths and
uses shorter alternatives in /tmp with MD5 hashing for uniqueness:
    /tmp/wt_daemon_a1b2c3d4.sock

Historical Debug Process
========================

The fix was discovered by examining daemon.log files in pytest temp directories:
    find /private/var/folders -name "daemon.log" -mmin -10

The logs clearly showed the daemon starting successfully but immediately failing
on socket creation. This is a common issue when running daemon tests with pytest.

Test Architecture
=================

These tests use proper isolation patterns:
- pytest's tmp_path fixture (not tempfile.TemporaryDirectory)
- kill_daemon_and_verify() with timeout-based verification
- Fixture teardown that ensures daemon cleanup
- WT_MAIN_REPO env var for config discovery
- Absolute path validation (no relative paths allowed)

The tests run the actual CLI binary end-to-end, making them true integration tests
that catch real-world deployment issues like this socket path problem.
"""

import os
from datetime import timedelta
from pathlib import Path

import pygit2
import pytest

from wt.testing.git_helpers import add_and_commit
from wt.testing.utils import wait_until

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


@pytest.mark.timeout(10)
def test_real_workflow_git_repo_to_worktrees_to_status(real_temp_repo, real_env, wt_cli):
    """
    Test workflow: git repo -> make worktrees 1,2 -> jump to worktree -> rm other -> status
    This tests the ACTUAL UNMODIFIED UNMOCKED program with real git operations.
    """
    # Step 1: Initial status (should show empty or main repo only)
    result = wt_cli.status(timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    # Step 2: Create first worktree
    result = wt_cli.sh_c("feature1", timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    # Verify worktree was actually created with real git
    worktree1_path = real_temp_repo / "worktrees" / "feature1"
    assert worktree1_path.exists(), f"Worktree 1 not created at {worktree1_path}"
    assert (worktree1_path / ".git").exists(), "Worktree 1 missing .git"

    # Verify git branch was created correctly using pygit2
    repo1 = pygit2.Repository(worktree1_path)
    current_branch = repo1.head.shorthand
    assert current_branch == "test/feature1", f"Expected test/feature1 branch, got: {current_branch}"

    # Step 3: Create second worktree
    result = wt_cli.sh_c("feature2", timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    # Verify second worktree was created
    worktree2_path = real_temp_repo / "worktrees" / "feature2"
    assert worktree2_path.exists(), f"Worktree 2 not created at {worktree2_path}"

    # Step 4: Check status shows both worktrees
    result = wt_cli.status(timeout=timedelta(seconds=10.0))
    assert result.returncode == 0
    assert "feature1" in result.stdout
    assert "feature2" in result.stdout

    # Step 5: Navigate to feature1 (test cd command emission)
    result = wt_cli.sh("feature1", timeout=timedelta(seconds=10.0))
    assert result.returncode == 0
    # Note: cd command is emitted to fd3, we can't easily verify it here

    # Step 6: Test real git operations in the worktree using pygit2
    add_and_commit(worktree1_path, {"test.txt": "Hello from feature1!"}, "Add test file")

    # Step 7: Final status check should show the changes
    result = wt_cli.status(timeout=timedelta(seconds=10.0))
    assert result.returncode == 0


def test_real_git_operations_in_worktrees(real_temp_repo, real_env, wt_cli):
    """Test that git operations work correctly in created worktrees."""

    # Create worktree
    result = wt_cli.sh_c("git-test")
    print(f"Create git-test worktree (exit={result.returncode}):\n{result.stdout}\n{result.stderr}")
    assert result.returncode == 0

    worktree_path = real_temp_repo / "worktrees" / "git-test"
    print(f"Expected worktree path: {worktree_path}")
    print(f"Worktree path exists: {worktree_path.exists()}")

    # Debug: check what's actually in the worktrees directory
    worktrees_parent = real_temp_repo / "worktrees"
    if worktrees_parent.exists():
        print(f"Contents of worktrees dir: {list(worktrees_parent.iterdir())}")
    else:
        print(f"Worktrees dir doesn't exist: {worktrees_parent}")

    # Debug: check for the worktree in the parent directory too
    alt_path = real_temp_repo.parent / "worktrees" / "git-test"
    print(f"Alt path exists: {alt_path} -> {alt_path.exists()}")

    assert worktree_path.exists()

    # Test git operations in the worktree using pygit2
    add_and_commit(worktree_path, {"test.txt": "Hello from worktree!"}, "Test commit")

    # Verify branch was created correctly
    wt_repo = pygit2.Repository(worktree_path)
    assert wt_repo.head.shorthand == "test/git-test"

    # Verify the file exists and has correct content
    test_file = worktree_path / "test.txt"
    assert test_file.exists()
    assert test_file.read_text() == "Hello from worktree!"

    # Verify commit was made
    commit = wt_repo.head.peel(pygit2.Commit)
    assert "Test commit" in commit.message


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
    assert wait_until(lambda: pid_file.exists(), timeout_seconds=2.0, interval_seconds=0.05)

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

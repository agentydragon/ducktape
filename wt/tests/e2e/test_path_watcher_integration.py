"""Integration test for path watcher - tests daemon through full worktree lifecycle.

Refactored to use wt_cli fixture for invoking the CLI.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from wt.shared.configuration import Configuration
from wt.shared.git_utils import git_run

from ..test_data import WATCHER_DEBOUNCE_SECS
from ..test_utils import wait_until


def _status(wt_cli) -> str:
    r = wt_cli.status(timeout=timedelta(seconds=5.0))
    assert r.returncode == 0, r.stderr
    return r.stdout


def wait_for_status_contains(wt_cli, needle: str, timeout: float = WATCHER_DEBOUNCE_SECS * 8) -> None:
    last = {"out": ""}
    ok = wait_until(
        lambda: (last.update({"out": _status(wt_cli)}), needle in last["out"])[-1],
        timeout_seconds=timeout,
        interval_seconds=WATCHER_DEBOUNCE_SECS,
    )
    if not ok:
        pytest.fail(f"Timed out waiting for status to contain '{needle}'. Last output:\n{last['out']}")


def wait_for_status_contains_all(wt_cli, needles: list[str], timeout: float = WATCHER_DEBOUNCE_SECS * 8) -> None:
    last = {"out": ""}
    ok = wait_until(
        lambda: (last.update({"out": _status(wt_cli)}), all(n in last["out"] for n in needles))[-1],
        timeout_seconds=timeout,
        interval_seconds=WATCHER_DEBOUNCE_SECS,
    )
    if not ok:
        missing = [n for n in needles if n not in last["out"]]
        pytest.fail(
            f"Timed out waiting for status to contain all {needles}. Missing: {missing}. Last output:\n{last['out']}"
        )


def wait_for_status_not_contains(wt_cli, needle: str, timeout: float = WATCHER_DEBOUNCE_SECS * 8) -> None:
    last = {"out": ""}
    ok = wait_until(
        lambda: (last.update({"out": _status(wt_cli)}), needle not in last["out"])[-1],
        timeout_seconds=timeout,
        interval_seconds=WATCHER_DEBOUNCE_SECS,
    )
    if not ok:
        pytest.fail(f"Timed out waiting for status to drop '{needle}'. Last output:\n{last['out']}")


@pytest.mark.timeout(30)
def test_path_watcher_full_lifecycle(wt_cli):
    """
    Test the path watcher through complete worktree lifecycle:
    1. status (should start daemon)
    2. create worktree
    3. status (should detect new worktree via path watcher)
    4. remove worktree
    5. status (should detect removal via path watcher)
    """

    # Resolve config to locate filesystem paths
    wt_dir = Path(wt_cli.env["WT_DIR"])
    config = Configuration.resolve(wt_dir)
    repo_path = Path(config.main_repo)

    # Step 1: Initial status - should start daemon and show empty state
    result = wt_cli.status(timeout=timedelta(seconds=5.0))
    assert result.returncode == 0, f"Status command failed: {result.stderr}"

    # Step 2: Create a worktree
    result = wt_cli.sh_c("feature-test", timeout=timedelta(seconds=5.0))
    assert result.returncode == 0, f"Create command failed: {result.stderr}"

    # Verify worktree was created on filesystem
    worktree_path = Path(config.worktrees_dir) / "feature-test"
    assert worktree_path.exists(), f"Worktree not created at {worktree_path}"
    assert worktree_path.is_dir(), f"Worktree path is not a directory: {worktree_path}"

    # Wait for watcher-driven status to reflect the new worktree
    wait_for_status_contains(wt_cli, "feature-test")

    # Step 3: Status should now show the new worktree (detected via path watcher)
    result = wt_cli.status(timeout=timedelta(seconds=5.0))
    assert result.returncode == 0, f"Status after create failed: {result.stderr}"
    assert "feature-test" in result.stdout, "New worktree not detected in status output"

    # Step 4: Remove the worktree
    result = wt_cli.sh("rm", "feature-test", "--force", timeout=timedelta(seconds=5.0))
    assert result.returncode == 0, f"Remove command failed: {result.stderr}"
    # Ensure git no longer lists the worktree (verifies git worktree remove succeeded)
    git_list = git_run(["worktree", "list"], cwd=repo_path)
    assert str(worktree_path) not in git_list.stdout.decode(), "Worktree still listed in main repo after removal"

    # Verify worktree was removed from filesystem
    assert not worktree_path.exists(), f"Worktree still exists after removal: {worktree_path}"

    # Wait for watcher to drop the worktree from status
    wait_for_status_not_contains(wt_cli, "feature-test")

    # Step 5: Status should no longer show the worktree (detected removal via path watcher)
    result = wt_cli.status(timeout=timedelta(seconds=5.0))
    assert result.returncode == 0, f"Status after remove failed: {result.stderr}"
    # Note: The daemon should detect that the worktree is gone and either:
    # 1. Not show it in status output, or
    # 2. Show it with an error state indicating it's missing
    # Either way, this tests that the path watcher is working


@pytest.mark.timeout(30)
def test_path_watcher_multiple_worktrees(wt_cli):
    """
    Test path watcher with multiple worktrees created and removed.
    Tests that the daemon can track multiple changes in sequence.
    """

    # Resolve config for filesystem checks
    wt_dir = Path(wt_cli.env["WT_DIR"])
    config = Configuration.resolve(wt_dir)
    repo_path = Path(config.main_repo)

    # Initial status to start daemon
    result = wt_cli.status(timeout=timedelta(seconds=5.0))
    assert result.returncode == 0

    worktree_names = ["wt1", "wt2", "wt3"]

    # Create multiple worktrees
    for name in worktree_names:
        result = wt_cli.sh_c(name, timeout=timedelta(seconds=5.0))
        assert result.returncode == 0, f"Failed to create {name}: {result.stderr}"

    # Wait for all worktrees to appear in status in one poll loop
    wait_for_status_contains_all(wt_cli, worktree_names)

    # Status should show all worktrees
    result = wt_cli.status(timeout=timedelta(seconds=5.0))
    assert result.returncode == 0
    for name in worktree_names:
        assert name in result.stdout, f"Worktree {name} not detected after creation"

    # Remove worktrees one by one
    remaining = worktree_names.copy()

    def _wait_until_removed(wt_cli, missing_name: str, timeout: float = 6.0):
        last = {"out": ""}

        def _is_removed() -> bool:
            result = wt_cli.status(timeout=timedelta(seconds=3.0))
            last["out"] = result.stdout
            return missing_name not in result.stdout

        ok = wait_until(_is_removed, timeout_seconds=timeout, interval_seconds=WATCHER_DEBOUNCE_SECS)
        if not ok:
            print(f"DEBUG last status while waiting removal of {missing_name}:\n{last['out']}")
        return ok

    for name in worktree_names:
        result = wt_cli.sh("rm", name, "--force", timeout=timedelta(seconds=5.0))
        assert result.returncode == 0, f"Failed to remove {name}: {result.stderr}"
        # Verify git no longer lists the worktree entry
        wt_path = Path(config.worktrees_dir) / name
        git_list = git_run(["worktree", "list"], cwd=repo_path)
        assert str(wt_path) not in git_list.stdout.decode(), f"Worktree {name} still listed in main repo after removal"
        remaining.remove(name)

        assert _wait_until_removed(wt_cli, name), f"Worktree {name} still present in status after removal"
        print(f"After removing {name}, remaining should be: {remaining}")

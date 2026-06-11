from datetime import timedelta

import pytest

from wt.testing.git_helpers import worktree_exists
from wt.testing.utils import wait_until

pytestmark = pytest.mark.timeout(20)


@pytest.mark.integration
def test_worktree_add_then_remove_reflected_in_status(wt_cli, pygit2_repo, real_temp_repo):
    # Initially, status should show no worktrees
    r0 = wt_cli.status(timeout=timedelta(seconds=10.0))
    assert r0.returncode == 0

    # Create a worktree via CLI
    name = "dyn-x"
    r1 = wt_cli.sh_c(name, timeout=timedelta(seconds=10.0))
    assert r1.returncode == 0

    # Poll until it appears
    assert wait_until(
        lambda: name in wt_cli.status(timeout=timedelta(seconds=10.0)).stdout,
        timeout_seconds=10.0,
        interval_seconds=0.2,
    ), "newly created worktree did not appear in status output"

    # Remove the worktree via CLI
    r2 = wt_cli.sh("rm", name, "--force", timeout=timedelta(seconds=15.0))
    assert r2.returncode == 0

    # Ensure git no longer lists the worktree (verifies git worktree remove)
    worktree_path = real_temp_repo / "worktrees" / name
    assert not worktree_exists(pygit2_repo, worktree_path), "Worktree still listed in main repo after removal"

    # Poll until it disappears
    assert wait_until(
        lambda: name not in wt_cli.status(timeout=timedelta(seconds=10.0)).stdout,
        timeout_seconds=10.0,
        interval_seconds=0.2,
    ), "deleted worktree still present in status output"

from datetime import timedelta
import shutil

import pytest

pytestmark = pytest.mark.integration


def test_manual_delete_of_old_worktree_does_not_break_new_create(real_temp_repo, wt_cli):
    """
    Repro for server crash when a previously-registered worktree directory was
    deleted out-of-band. Creating a new worktree should not fail with
    "Repository not found at <stale path>".
    """
    # real_env fixture ensures clean daemon state per WT_DIR

    # 1) Create an initial worktree
    name_old = "stale-old"
    r1 = wt_cli.sh_c(name_old, timeout=timedelta(seconds=20.0))
    assert r1.returncode == 0, f"Initial create failed: {r1.stderr}"

    wt_old = real_temp_repo / "worktrees" / name_old
    assert wt_old.exists()

    # 2) Manually delete the directory (simulate out-of-band removal)
    shutil.rmtree(wt_old, ignore_errors=True)
    assert not wt_old.exists()

    # 3) Create a new worktree; previously this crashed on list_worktrees()
    name_new = "after-stale"
    r2 = wt_cli.sh_c(name_new, timeout=timedelta(seconds=20.0))
    assert r2.returncode == 0, (
        f"New worktree creation failed (likely due to stale entry crash):\nstdout=\n{r2.stdout}\nstderr=\n{r2.stderr}"
    )

    wt_new = real_temp_repo / "worktrees" / name_new
    assert wt_new.exists(), f"New worktree not created at {wt_new}"

    # Optional: a quick status call should succeed and not mention errors
    r3 = wt_cli.status(timeout=timedelta(seconds=20.0))
    assert r3.returncode == 0

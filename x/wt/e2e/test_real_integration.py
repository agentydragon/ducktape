"""Real integration tests for actual CLI."""

from datetime import timedelta

import pytest_bazel

from x.wt.testing.git_helpers import worktree_exists


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


if __name__ == "__main__":
    pytest_bazel.main()

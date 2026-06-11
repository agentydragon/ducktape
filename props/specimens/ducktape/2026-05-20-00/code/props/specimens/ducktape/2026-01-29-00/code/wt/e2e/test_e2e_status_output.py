"""E2E test: create multiple worktrees and verify `wt status` output.

Uses the real CLI via subprocess with completely isolated WT_DIR per test
through the existing `real_env` fixture and helpers.
"""

from datetime import timedelta
from pathlib import Path

import pytest
import pytest_bazel

from wt.testing.asserts import assert_output_contains, extract_status_rows, status_row_ok
from wt.testing.utils import wait_until

pytestmark = pytest.mark.timeout(10)


@pytest.mark.integration
def test_status_lists_multiple_worktrees(real_temp_repo, wt_cli):
    """Create two worktrees and ensure `wt sh` status output reflects them."""

    # Initial status should succeed; header should include component summary
    result = wt_cli.status(timeout=timedelta(seconds=10.0))
    assert result.returncode == 0
    assert_output_contains(result.stdout, "gitstatusd")

    # Create first worktree
    result = wt_cli.sh_c("alpha", timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    # Verify created on disk
    wt1 = Path(real_temp_repo) / "worktrees" / "alpha"
    assert wt1.exists()
    assert (wt1 / ".git").exists()

    # Create second worktree
    result = wt_cli.sh_c("beta", timeout=timedelta(seconds=10.0))
    assert result.returncode == 0

    wt2 = Path(real_temp_repo) / "worktrees" / "beta"
    assert wt2.exists()
    assert (wt2 / ".git").exists()

    # Poll until both worktrees are reported as clean and running, and commit column is hex
    last = {"out": ""}

    def _both_ok() -> bool:
        res = wt_cli.status(timeout=timedelta(seconds=3.0))
        assert res.returncode == 0
        last["out"] = res.stdout
        rows = extract_status_rows(last["out"])
        l1 = rows.get("alpha")
        l2 = rows.get("beta")
        return bool(l1 and l2 and status_row_ok(l1) and status_row_ok(l2))

    ok = wait_until(_both_ok, timeout_seconds=5.0, interval_seconds=0.2)
    if not ok:
        raise AssertionError(
            f"Status did not reach clean/running with hex commit for both worktrees.\nLast output:\n{last['out']}"
        )


if __name__ == "__main__":
    pytest_bazel.main()

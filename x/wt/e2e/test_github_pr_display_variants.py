"""E2E: real daemon/client with WT_TEST_MODE PR fixtures; PR variants: open(can merge), merged, closed, no PR."""

import json
import os
import re
import socket
import uuid
from datetime import timedelta
from typing import Any

import pytest
import pytest_bazel

from x.wt.shared.fixtures import PRFixtureEntry
from x.wt.shared.github_models import PRState
from x.wt.testing.asserts import assert_output_contains, extract_status_rows


def _rpc_json(sock_path: str | os.PathLike, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Minimal JSON-RPC 2.0 call helper for tests over UNIX socket."""
    req = {"jsonrpc": "2.0", "method": method, "params": params, "id": str(uuid.uuid4())}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(sock_path))
        with s.makefile("rwb") as f:
            payload = (json.dumps(req) + "\n").encode()
            f.write(payload)
            f.flush()
            line = f.readline()
            if not line:
                raise AssertionError("No response from daemon")
            result: dict[str, Any] = json.loads(line.decode())
            return result


@pytest.mark.parametrize(
    ("pr", "expects"),
    [
        pytest.param(
            PRFixtureEntry(number=123, state=PRState.OPEN, mergeable=True, additions=10, deletions=2),
            ["#123", "can merge", "+10/-2"],
            id="open_mergeable",
        ),
        pytest.param(
            PRFixtureEntry(
                number=456,
                state=PRState.CLOSED,
                mergeable=True,
                merged_at="2024-01-15T10:30:00",
                additions=3,
                deletions=1,
            ),
            ["#456", "merged", "+3/-1"],
            id="merged",
        ),
        pytest.param(
            PRFixtureEntry(number=789, state=PRState.CLOSED, mergeable=False, additions=4, deletions=4),
            ["#789", "closed", "+4/-4"],
            id="closed",
        ),
        pytest.param(None, [], id="none"),
    ],
)
def test_github_pr_variants(pr, expects, real_temp_repo, daemon_config_factory, write_pr_fixtures, wt_cli):
    # Rewrites config.yaml in the WT_DIR `wt_cli` is bound to, before the first CLI call starts the daemon.
    config = daemon_config_factory(real_temp_repo).integration(github_repo="test/test")
    # PR fixtures are read by the daemon under WT_TEST_MODE
    write_pr_fixtures(config, {} if pr is None else {"feature-x": pr, "*": pr})

    # Start daemon
    r1 = wt_cli.status(timeout=timedelta(seconds=30.0))
    assert r1.returncode == 0

    # Create a worktree and wait for PR display
    r2 = wt_cli.sh_c("feature-x", timeout=timedelta(seconds=30.0))
    assert r2.returncode == 0

    # Lookup wtid and force a PR refresh synchronously via RPC to avoid polling
    wt_by_name = _rpc_json(config.daemon_socket_path, "worktree_get_by_name", {"name": "feature-x"})
    wtid = wt_by_name["result"]["wtid"]
    assert wtid, "Server did not return wtid for created worktree"
    refresh_res = _rpc_json(config.daemon_socket_path, "pr_refresh_now", {"wtid": wtid})
    assert refresh_res.get("result") == "ok"

    # Render once and assert
    status_result = wt_cli.status(timeout=timedelta(seconds=30.0))
    assert status_result.returncode == 0, status_result.stderr
    # The row, not the whole output: the output also prints the daemon log path, which names the pytest test id.
    row = extract_status_rows(status_result.stdout)["feature-x"]
    assert_output_contains(row, *expects)
    if pr is None:
        assert not re.search(r"#\d+", row)


if __name__ == "__main__":
    pytest_bazel.main()

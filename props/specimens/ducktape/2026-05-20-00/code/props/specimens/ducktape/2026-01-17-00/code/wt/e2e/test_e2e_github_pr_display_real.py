"""E2E test: real wt daemon/client, mocked PyGithub via import shadowing, assert PR is shown.

Uses real repo/daemon environment; we inject a temporary 'github' module on PYTHONPATH
so the daemon imports our stub instead of the real PyGithub. This avoids network while
exercising the full daemon/CLI pipeline.
"""

import json
import os
import re
import socket
import uuid
from datetime import timedelta
from typing import Any

import pytest

from wt.shared.fixtures import PRFixtureEntry
from wt.shared.github_models import PRState
from wt.testing.utils import wait_until

# Global conftest disables gh token via get_github_token


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


@pytest.mark.integration
@pytest.mark.real_github
def test_github_pr_display_with_mocked_pygithub(real_temp_repo, config_factory, tmp_path, write_pr_fixtures, wt_cli):
    # Prepare config with GitHub enabled
    factory = config_factory(real_temp_repo)
    config = factory.integration(github_enabled=True, github_repo="test/test")

    # Build environment inheriting system env to ensure click, etc. are available
    env = os.environ.copy()
    env["WT_DIR"] = str(config.wt_dir)
    # Prepend our mock to PYTHONPATH so daemon imports it; also include project root
    # WT_TEST_MODE fixtures — replace PYTHONPATH shadowing
    pr_entry = PRFixtureEntry(
        number=123, state=PRState.OPEN, draft=False, mergeable=True, merged_at=None, additions=10, deletions=2
    )
    pr_map = {
        "feature-x": pr_entry,
        "*": pr_entry,  # Allow any branch prefix (e.g., 'test/feature-x')
    }
    # Use shared fixture helper
    write_pr_fixtures(config, pr_map)

    # Start daemon implicitly by running status once
    wt_cli.env = env
    out = wt_cli.status(timeout=timedelta(seconds=30.0))
    assert out.returncode == 0

    # Create a worktree with branch 'feature-x'
    out2 = wt_cli.sh_c("feature-x", timeout=timedelta(seconds=30.0))
    assert out2.returncode == 0

    # Force a PR refresh synchronously via RPC to avoid polling flakiness
    wt_by_name = _rpc_json(config.daemon_socket_path, "worktree_get_by_name", {"name": "feature-x"})
    wtid = wt_by_name["result"]["wtid"]
    assert wtid, "Server did not return wtid for created worktree"
    refresh_res = _rpc_json(config.daemon_socket_path, "pr_refresh_now", {"wtid": wtid})
    assert refresh_res.get("result") == "ok"

    # Now poll until status shows the PR details (robust to any async)
    last = {"out": ""}

    def _status_has_pr() -> bool:
        result = wt_cli.status(timeout=timedelta(seconds=30.0))
        last["out"] = result.stdout
        return (
            "#123" in result.stdout and bool(re.search(r"\\bcan merge\\b", result.stdout)) and "+10/-2" in result.stdout
        )

    ok = wait_until(_status_has_pr, timeout_seconds=12.0, interval_seconds=0.25)
    if not ok:
        raise AssertionError(f"PR details not shown in time. Last output:\n{last['out']}")

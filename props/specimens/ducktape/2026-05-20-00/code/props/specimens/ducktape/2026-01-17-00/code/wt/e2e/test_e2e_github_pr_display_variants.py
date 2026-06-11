"""E2E: real daemon/client + shadowed PyGithub; PR variants: open(can merge), merged, closed, no PR."""

import json
import os
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wt.shared.fixtures import PRFixtureEntry
from wt.shared.github_models import PRState


def _write_shadow_github(mock_root: Path, variant: str):
    mock_pkg = mock_root / "github"
    mock_pkg.mkdir(parents=True, exist_ok=True)
    if variant == "open_mergeable":
        body = """
from datetime import timedelta, datetime
from types import SimpleNamespace
class Github:
    def __init__(self, *args, **kwargs):
        pass
    def get_repo(self, full_name):
        def get_pull(number):
            return SimpleNamespace(number=123, state="open", draft=False, mergeable=True, merged_at=None, additions=10, deletions=2)
        return SimpleNamespace(get_pull=get_pull)
    def search_issues(self, q):
        return [SimpleNamespace(number=123)]
"""
    elif variant == "merged":
        body = """
from types import SimpleNamespace
import datetime
class Github:
    def __init__(self, *args, **kwargs):
        pass
    def get_repo(self, full_name):
        def get_pull(number):
            return SimpleNamespace(
                number=456,
                state="closed",
                draft=False,
                mergeable=True,
                merged_at=datetime.datetime.now(),
                additions=3,
                deletions=1,
            )
        return SimpleNamespace(get_pull=get_pull)
    def search_issues(self, q):
        return [SimpleNamespace(number=456)]
"""
    elif variant == "closed":
        body = """
from types import SimpleNamespace
class Github:
    def __init__(self, *args, **kwargs):
        pass
    def get_repo(self, full_name):
        def get_pull(number):
            return SimpleNamespace(
                number=789,
                state="closed",
                draft=False,
                mergeable=False,
                merged_at=None,
                additions=4,
                deletions=4,
            )
        return SimpleNamespace(get_pull=get_pull)
    def search_issues(self, q):
        return [SimpleNamespace(number=789)]
"""
    elif variant == "none":
        body = """
from types import SimpleNamespace
from datetime import timedelta, datetime
class Github:
    def __init__(self, *args, **kwargs):
        pass
    def get_repo(self, full_name):
        return SimpleNamespace()
    def search_issues(self, q):
        return []
"""
    else:
        raise ValueError("unknown variant")
    (mock_pkg / "__init__.py").write_text(body)


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
@pytest.mark.parametrize(
    ("variant", "expects"),
    [
        ("open_mergeable", ["#123", "can merge", "+10/-2"]),
        ("merged", ["#456", "merged", "+3/-1"]),
        ("closed", ["#789", "closed", "+4/-4"]),
        ("none", []),
    ],
)
def test_github_pr_variants(variant, expects, github_pr_env: "GithubPrEnv"):
    env = github_pr_env
    factory = env.config_factory(env.repo_path)
    config = factory.integration(github_enabled=True, github_repo="test/test")
    wt_cli = env.wt_cli
    write_pr_fixtures = env.write_pr_fixtures

    test_env = os.environ.copy()
    test_env["WT_DIR"] = str(config.wt_dir)
    # Bind wt_cli to this test's WT_DIR/config
    wt_cli.env = test_env
    # Write PR fixtures for WT_TEST_MODE to avoid PYTHONPATH hacks
    if variant == "none":
        pr_map = {}
    else:
        entry = PRFixtureEntry(
            number=123
            if variant == "open_mergeable"
            else 456
            if variant == "merged"
            else 789
            if variant == "closed"
            else 0,
            state=PRState.OPEN if variant == "open_mergeable" else PRState.CLOSED,
            draft=False,
            mergeable=variant in {"open_mergeable", "merged"},
            merged_at=None if variant != "merged" else datetime.now().isoformat(),
            additions=10
            if variant == "open_mergeable"
            else 3
            if variant == "merged"
            else 4
            if variant == "closed"
            else 0,
            deletions=2
            if variant == "open_mergeable"
            else 1
            if variant == "merged"
            else 4
            if variant == "closed"
            else 0,
        )
        pr_map = {"feature-x": entry, "*": entry}
    # Use shared fixture helper to write Pydantic-validated map
    write_pr_fixtures(config, pr_map)

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
    out = status_result.stdout
    if expects:
        for x in expects:
            assert x in out
    else:
        # No PR should render no #<n>
        assert not re.search(r"#\d+", out)


# Global conftest disables gh token via get_github_token


@dataclass(frozen=True)
class GithubPrEnv:
    repo_path: Path
    config_factory: Any
    tmp_path: Path
    write_pr_fixtures: Any
    wt_cli: Any


@pytest.fixture
def github_pr_env(real_temp_repo, config_factory, tmp_path, write_pr_fixtures, wt_cli) -> GithubPrEnv:
    return GithubPrEnv(
        repo_path=real_temp_repo,
        config_factory=config_factory,
        tmp_path=tmp_path,
        write_pr_fixtures=write_pr_fixtures,
        wt_cli=wt_cli,
    )

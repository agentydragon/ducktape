import json
import pathlib
import re
import subprocess
from collections.abc import Callable

import httpx
import pytest
import pytest_bazel
import respx

from devinfra.gc import workspace_gc, workspace_scan
from devinfra.gc.conftest import GitRepo


def test_main_routes_options_to_default_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_app(*, args: list[str]) -> None:
        calls.append(args)

    monkeypatch.setattr(workspace_gc, "app", fake_app)

    workspace_gc.main(["--no-prs"])

    assert calls == [["all", "--no-prs"]]


_GRAPHQL_URL = "https://api.github.com/graphql"
_ALIAS_RE = re.compile(r"\b(b\d+): pullRequests\(")


@pytest.fixture
def github_repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A repo whose origin is a GitHub remote, with a token in the environment.

    `pr_states` reads both through its own helpers, so nothing about credential or slug
    discovery is stubbed — only the HTTP boundary below is.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/owner/name"], check=True)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    return repo


def _in_turn(*payloads: Callable[[httpx.Request], dict]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer successive requests from `payloads`, so each batch gets its own outcome."""
    remaining = iter(payloads)
    return lambda request: httpx.Response(200, json=next(remaining)(request))


def _always_merged(request: httpx.Request) -> httpx.Response:
    """Every batch resolves — the quota never runs out."""
    return httpx.Response(200, json=_merged_nodes_for(request))


def _rate_limited(_request: httpx.Request) -> dict:
    return {"data": None, "errors": [{"type": "RATE_LIMIT", "message": "exceeded"}]}


def _not_found(_request: httpx.Request) -> dict:
    return {"data": None, "errors": [{"type": "NOT_FOUND"}]}


def _merged_nodes_for(request: httpx.Request) -> dict:
    """Answer every alias the query actually asked for, with one merged PR each."""
    aliases = _ALIAS_RE.findall(json.loads(request.content)["query"])
    nodes = {alias: {"nodes": [{"number": 1, "state": "MERGED", "headRefOid": "deadbeef"}]} for alias in aliases}
    return {"data": {"repository": nodes}}


# More branches than one request carries, so the sweep really does batch and the second
# batch is a separate round trip that can fail on its own.
_BRANCHES = {f"branch-{i:03d}" for i in range(workspace_gc._GRAPHQL_BATCH + 10)}


@respx.mock
def test_pr_states_keeps_batches_resolved_before_a_rate_limit(github_repo: pathlib.Path) -> None:
    """Quota exhaustion mid-sweep must not discard the PRs already resolved.

    The limit is hourly and shared with every other GitHub caller, so it is an ambient
    condition rather than a repo problem: the branches already answered stay answered and the
    rest fall back to git signals.
    """
    respx.post(_GRAPHQL_URL).mock(side_effect=_in_turn(_merged_nodes_for, _rate_limited))

    states = workspace_gc.pr_states(github_repo, _BRANCHES)

    # The first batch survived; the second was abandoned rather than taking the whole sweep
    # down with it.
    assert len(states) == workspace_gc._GRAPHQL_BATCH


@respx.mock
def test_pr_states_still_reports_other_graphql_errors(github_repo: pathlib.Path) -> None:
    """A non-quota GraphQL failure is not the resilient case and must not be silently kept."""
    respx.post(_GRAPHQL_URL).mock(side_effect=_in_turn(_merged_nodes_for, _not_found))

    assert workspace_gc.pr_states(github_repo, _BRANCHES) == {}


@respx.mock
def test_pr_states_resolves_every_branch_when_the_quota_holds(github_repo: pathlib.Path) -> None:
    """The happy path still sweeps every batch — the anchor for the two failure cases."""
    respx.post(_GRAPHQL_URL).mock(side_effect=_always_merged)

    assert workspace_gc.pr_states(github_repo, _BRANCHES).keys() == _BRANCHES


# Branch names that look like numbers: `tabulate` rewrites a column whose every cell does.
_NUMERIC_LOOKING_BRANCHES = ["1e5", "2.10", "1.0", "007"]


def _scan_numeric_looking_branches(
    repo: GitRepo, proc: pathlib.Path, mountinfo: pathlib.Path
) -> workspace_scan.WorkspaceScan:
    for index, branch in enumerate(_NUMERIC_LOOKING_BRANCHES):
        repo.worktree(f"wt{index}", branch)  # an empty branch: its worktree and branch are both prunable
    return workspace_scan.scan_workspace(
        repo.path, main="main", default_branch="main", pr_states={}, proc_root=proc, mountinfo_path=mountinfo
    )


def _cells(report: str, column: int) -> list[str]:
    """One column of a rendered table's rows: not its header line, nor its trailing summary line."""
    return sorted(line.split()[column] for line in report.splitlines()[1:-1])


def test_render_worktrees_prints_numeric_looking_branch_names_verbatim(
    repo: GitRepo, proc: pathlib.Path, mountinfo: pathlib.Path
) -> None:
    scan = _scan_numeric_looking_branches(repo, proc, mountinfo)

    report = workspace_gc.render_worktrees(scan.worktrees, include_kept=False)

    # STATUS, LAST ACTIVITY, WORKTREE, BRANCH: the fourth cell of a row.
    assert _cells(report, 3) == sorted(_NUMERIC_LOOKING_BRANCHES)


def test_render_branches_prints_numeric_looking_branch_names_verbatim(
    repo: GitRepo, proc: pathlib.Path, mountinfo: pathlib.Path
) -> None:
    scan = _scan_numeric_looking_branches(repo, proc, mountinfo)

    report = workspace_gc.render_branches(scan.branches, include_kept=False)

    # STATUS, BRANCH: the second cell of a row. The default branch is kept, so it is not a row.
    assert _cells(report, 1) == sorted(_NUMERIC_LOOKING_BRANCHES)


if __name__ == "__main__":
    pytest_bazel.main()

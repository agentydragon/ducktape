import asyncio
import json
import pathlib
import re
import subprocess
from collections.abc import Callable

import httpx
import pytest
import pytest_bazel
import respx

from devinfra.gc import github_prs

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


def _merged_nodes_for(request: httpx.Request) -> dict:
    """Answer every alias the query actually asked for, with one merged PR each."""
    aliases = _ALIAS_RE.findall(json.loads(request.content)["query"])
    nodes = {alias: {"nodes": [{"number": 1, "state": "MERGED", "headRefOid": "deadbeef"}]} for alias in aliases}
    return {"data": {"repository": nodes}}


def _always_merged(request: httpx.Request) -> httpx.Response:
    """Every batch resolves — the quota never runs out."""
    return httpx.Response(200, json=_merged_nodes_for(request))


def _refuse_batch_containing(branch: str, refusal: dict) -> Callable[[httpx.Request], httpx.Response]:
    """Refuse the one batch that asks about `branch`; every other batch resolves."""

    def handler(request: httpx.Request) -> httpx.Response:
        if json.dumps(branch) in json.loads(request.content)["query"]:
            return httpx.Response(200, json=refusal)
        return _always_merged(request)

    return handler


async def test_github_token_prefers_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    monkeypatch.setenv("PATH", "")  # `gh` must not even be looked up

    assert await github_prs._github_token() == "env-token"


async def test_github_token_falls_back_to_gh_auth_token(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\n[ "$*" = "auth token" ] && echo gh-token\n')
    gh.chmod(0o755)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert await github_prs._github_token() == "gh-token"


async def test_github_token_is_none_when_gh_is_missing_or_fails(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # no `gh` here
    assert await github_prs._github_token() is None

    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    assert await github_prs._github_token() is None


_RATE_LIMITED = {"data": None, "errors": [{"type": "RATE_LIMIT", "message": "exceeded"}]}
_NOT_FOUND = {"data": None, "errors": [{"type": "NOT_FOUND"}]}

# More branches than one request carries, so the sweep really does batch and the second
# batch is a separate round trip that can fail on its own. Sorted, the first batch is
# branch-000..049 and the second branch-050..059.
_BRANCHES = {f"branch-{i:03d}" for i in range(github_prs._GRAPHQL_BATCH + 10)}
_FIRST_BATCH = {branch for branch in _BRANCHES if branch < "branch-050"}
_SECOND_BATCH = _BRANCHES - _FIRST_BATCH


@respx.mock
async def test_pr_states_keeps_batches_that_resolved_when_another_hits_a_rate_limit(github_repo: pathlib.Path) -> None:
    """Quota exhaustion in one batch must not discard the PRs the other batches resolved.

    The limit is hourly and shared with every other GitHub caller, so it is an ambient
    condition rather than a repo problem: the branches already answered stay answered and the
    rest fall back to git signals. Batches run concurrently, so which batch is refused is
    independent of which one finished first.
    """
    respx.post(_GRAPHQL_URL).mock(side_effect=_refuse_batch_containing("branch-055", _RATE_LIMITED))
    assert (await github_prs.pr_states(github_repo, _BRANCHES)).keys() == _FIRST_BATCH

    respx.post(_GRAPHQL_URL).mock(side_effect=_refuse_batch_containing("branch-000", _RATE_LIMITED))
    assert (await github_prs.pr_states(github_repo, _BRANCHES)).keys() == _SECOND_BATCH


@respx.mock
async def test_pr_states_still_reports_other_graphql_errors(github_repo: pathlib.Path) -> None:
    """A non-quota GraphQL failure is not the resilient case and must not be silently kept."""
    respx.post(_GRAPHQL_URL).mock(side_effect=_refuse_batch_containing("branch-055", _NOT_FOUND))

    assert await github_prs.pr_states(github_repo, _BRANCHES) == {}


@respx.mock
async def test_pr_states_resolves_every_branch_when_the_quota_holds(github_repo: pathlib.Path) -> None:
    """The happy path still sweeps every batch — the anchor for the two failure cases."""
    respx.post(_GRAPHQL_URL).mock(side_effect=_always_merged)

    assert (await github_prs.pr_states(github_repo, _BRANCHES)).keys() == _BRANCHES


@respx.mock
async def test_pr_states_has_several_batches_in_flight_at_once(github_repo: pathlib.Path) -> None:
    both_in_flight = asyncio.Barrier(2)  # a serial sweep would never get a second request to the barrier

    async def handler(request: httpx.Request) -> httpx.Response:
        async with asyncio.timeout(5):
            await both_in_flight.wait()
        return _always_merged(request)

    respx.post(_GRAPHQL_URL).mock(side_effect=handler)

    assert (await github_prs.pr_states(github_repo, _BRANCHES)).keys() == _BRANCHES


@respx.mock
async def test_pr_states_never_exceeds_the_concurrency_bound(github_repo: pathlib.Path) -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)  # yield so other requests can start while this one is "open"
        in_flight -= 1
        return _always_merged(request)

    respx.post(_GRAPHQL_URL).mock(side_effect=handler)
    many = {f"branch-{i:04d}" for i in range(github_prs._GRAPHQL_BATCH * 12)}

    assert (await github_prs.pr_states(github_repo, many)).keys() == many
    assert peak <= github_prs._MAX_CONCURRENT_REQUESTS


if __name__ == "__main__":
    pytest_bazel.main()

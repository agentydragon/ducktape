"""GitHub pull-request state for local branches, fetched with batched GraphQL queries.

`workspace_gc` injects the result into the offline classifiers as `dict[str, PrInfo]`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from devinfra.gc import git_repo
from devinfra.gc.pull_request import PrInfo, PrState

logger = logging.getLogger(__name__)

_PR_RANK = {PrState.MERGED: 3, PrState.OPEN: 2, PrState.CLOSED: 1}


def _github_token() -> str | None:
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    try:
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return None
    return token or None


_REMOTE_SLUG_RE = re.compile(r"(?:github\.com[:/])([^/]+/[^/]+?)(?:\.git)?/?$")


def _repo_slug(repo: Path) -> str | None:
    """The `owner/name` GitHub slug of `repo`'s `origin` remote, for the GraphQL PR lookup.

    GitHub-specific and `origin`-specific on purpose — this only feeds the PR API call, unlike
    `git_repo.shares_a_remote`'s host-and-remote-agnostic match used to find foreign clones.
    """
    pg = git_repo.open_repo(repo)
    try:
        url = pg.remotes["origin"].url
    except KeyError:
        return None
    if url is None:
        return None
    match = _REMOTE_SLUG_RE.search(url)
    return match.group(1) if match else None


_GRAPHQL_URL = "https://api.github.com/graphql"
_GRAPHQL_BATCH = 50  # aliased pullRequests fields per request; well under GraphQL node limits
_MAX_CONCURRENT_REQUESTS = 4  # kept low: GitHub's secondary rate limits penalize bursts of concurrent requests


def _pr_query(owner: str, name: str, branches: list[str]) -> tuple[str, dict[str, str]]:
    """A GraphQL query fetching each branch's PRs under an alias, plus the alias→branch map."""
    alias_to_branch = {f"b{i}": branch for i, branch in enumerate(branches)}
    fields = " ".join(
        f"{alias}: pullRequests(headRefName: {json.dumps(branch)}, first: 5, "
        f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{ nodes {{ number state headRefOid }} }}"
        for alias, branch in alias_to_branch.items()
    )
    query = f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }}"
    return query, alias_to_branch


def _most_decisive(nodes: Iterable[Mapping[str, Any]]) -> PrInfo | None:
    """The merged > open > closed PR among `nodes` (a branch's `pullRequests.nodes`)."""
    best: PrInfo | None = None
    for node in nodes:
        info = PrInfo(number=node["number"], state=PrState(node["state"]), head_sha=node["headRefOid"])
        if best is None or _PR_RANK[info.state] > _PR_RANK[best.state]:
            best = info
    return best


@dataclass(frozen=True, slots=True)
class _QuotaExhausted:
    """A batch GitHub refused with RATE_LIMIT."""


async def _fetch_batch(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    owner: str,
    name: str,
    batch: Sequence[str],
    *,
    index: int,
    total: int,
) -> dict[str, PrInfo] | _QuotaExhausted:
    async with semaphore:
        logger.info("Checking GitHub PR state batch %d/%d (%d branches)", index, total, len(batch))
        query, alias_to_branch = _pr_query(owner, name, list(batch))
        payload = (await client.post(_GRAPHQL_URL, json={"query": query})).raise_for_status().json()
    data = payload.get("data")
    if not data or data.get("repository") is None:
        errors = payload.get("errors") or []
        if any(error.get("type") == "RATE_LIMIT" for error in errors):
            return _QuotaExhausted()
        raise RuntimeError(f"GraphQL returned no data: {errors}")
    repository = data["repository"]
    states: dict[str, PrInfo] = {}
    for alias, branch in alias_to_branch.items():
        connection = repository.get(alias)
        if connection is not None and (info := _most_decisive(connection["nodes"])) is not None:
            states[branch] = info
    logger.info("Finished GitHub PR state batch %d/%d", index, total)
    return states


async def pr_states(repo: Path, branches: set[str]) -> dict[str, PrInfo]:
    """Most-decisive PR (merged > open > closed) per branch, via batched GraphQL queries.

    Aliased `pullRequests(headRefName:)` fields fetch ~50 branches per request instead of a
    REST call per branch — a few round-trips rather than hundreds. Up to
    `_MAX_CONCURRENT_REQUESTS` batches are in flight at once. `headRefName` matches PR
    records, so a branch whose remote ref was deleted after merge still resolves. Returns {}
    when there is no token, no GitHub remote, or any batch fails with a GraphQL or HTTP error
    other than a quota refusal. A batch refused for quota (RATE_LIMIT) is dropped and every
    batch that did resolve is kept — with concurrent batches, "resolved" means resolved at all,
    not resolved before the limit hit — so the caller classifies the rest on git signals alone.
    """
    if not branches:
        return {}
    slug = _repo_slug(repo)
    token = await asyncio.to_thread(_github_token)
    if slug is None:
        logger.warning("PR check skipped: origin is not a GitHub remote")
        return {}
    if token is None:
        logger.warning("PR check skipped: no GITHUB_TOKEN and `gh auth token` unavailable")
        return {}
    owner, name = slug.split("/", 1)
    headers = {"Authorization": f"bearer {token}", "User-Agent": "workspace-gc"}
    batches = list(itertools.batched(sorted(branches), _GRAPHQL_BATCH, strict=False))
    logger.info("Checking GitHub PR state for %d branches in %d batches", len(branches), len(batches))
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as client, asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(_fetch_batch(client, semaphore, owner, name, batch, index=index, total=len(batches)))
                for index, batch in enumerate(batches, start=1)
            ]
    except Exception:
        logger.warning("PR check skipped: GitHub API error", exc_info=True)
        return {}
    states: dict[str, PrInfo] = {}
    exhausted = 0
    for task in tasks:
        result = task.result()
        if isinstance(result, _QuotaExhausted):
            exhausted += 1
        else:
            states.update(result)
    if exhausted:
        # Quota is per-hour and shared with every other GitHub caller, so it says nothing about
        # this repo: keep the batches that resolved and let the rest classify on git signals.
        logger.warning(
            "PR check incomplete: GitHub GraphQL quota exhausted for %d of %d batches; "
            "classified %d of %d branches with PR data",
            exhausted,
            len(batches),
            len(states),
            len(branches),
        )
    else:
        logger.info("GitHub PR state complete: %d/%d branches resolved", len(states), len(branches))
    return states

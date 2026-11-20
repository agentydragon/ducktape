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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from devinfra.gc import git_repo
from devinfra.gc.async_io import run_blocking, run_process
from devinfra.gc.pull_request import PrInfo, PrLookup, PrState

logger = logging.getLogger(__name__)

_PR_RANK = {PrState.OPEN: 3, PrState.MERGED: 2, PrState.CLOSED: 1}


async def _github_token() -> str | None:
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    try:
        process = await run_process("gh", "auth", "token")
    except OSError:
        return None
    if process.returncode != 0:
        return None
    return process.stdout.strip() or None


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
PR_BATCH_SIZE = 50  # aliased pullRequests fields per request; well under GraphQL node limits
MAX_CONCURRENT_REQUESTS = 4  # GitHub's secondary rate limits penalize bursts of concurrent requests


def pr_batches(branches: Iterable[str]) -> tuple[frozenset[str], ...]:
    """Split branch names into the finite requests accepted by the GraphQL query."""
    return tuple(frozenset(batch) for batch in itertools.batched(sorted(set(branches)), PR_BATCH_SIZE, strict=False))


def _pr_query(owner: str, name: str, branches: list[str]) -> tuple[str, dict[str, str]]:
    """A GraphQL query fetching each branch's PRs under an alias, plus the alias→branch map."""
    alias_to_branch = {f"b{i}": branch for i, branch in enumerate(branches)}
    fields = " ".join(
        f"{alias}: pullRequests(headRefName: {json.dumps(branch)}, first: 5, "
        f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{ nodes {{ number state headRefOid }} "
        "pageInfo { hasNextPage } }"
        for alias, branch in alias_to_branch.items()
    )
    query = f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }}"
    return query, alias_to_branch


def _most_decisive(nodes: Iterable[Mapping[str, Any]]) -> PrInfo | None:
    """Prefer OPEN, then MERGED, then CLOSED among a branch's returned PRs."""
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
) -> PrLookup | _QuotaExhausted:
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
    unknown: set[str] = set()
    for alias, branch in alias_to_branch.items():
        connection = repository.get(alias)
        if connection is None:
            unknown.add(branch)
            continue
        nodes = connection["nodes"]
        info = _most_decisive(nodes)
        if connection["pageInfo"]["hasNextPage"] and not any(node["state"] == PrState.OPEN for node in nodes):
            # An unseen open PR could change the decision. A visible open PR is already
            # sufficient to keep the branch, regardless of any older merged/closed PR.
            unknown.add(branch)
        elif info is not None:
            states[branch] = info
    logger.info("Finished GitHub PR state batch %d/%d", index, total)
    return PrLookup(states, unknown)


async def pr_states(repo: Path, branches: Iterable[str]) -> PrLookup:
    """Most-decisive PR (open > merged > closed) per branch, via batched GraphQL queries.

    Aliased `pullRequests(headRefName:)` fields fetch ~50 branches per request instead of a
    REST call per branch — a few round-trips rather than hundreds. Up to
    `MAX_CONCURRENT_REQUESTS` batches are in flight at once. `headRefName` matches PR
    records, so a branch whose remote ref was deleted after merge still resolves. A failed
    lookup is distinct from a successful lookup finding no PR: its branches are unknown.
    """
    branches = set(branches)
    if not branches:
        return PrLookup({})
    slug = await run_blocking(_repo_slug, repo)
    token = await _github_token()
    if slug is None:
        logger.warning("PR check skipped: origin is not a GitHub remote")
        return PrLookup({}, branches)
    if token is None:
        logger.warning("PR check skipped: no GITHUB_TOKEN and `gh auth token` unavailable")
        return PrLookup({}, branches)
    owner, name = slug.split("/", 1)
    headers = {"Authorization": f"bearer {token}", "User-Agent": "workspace-gc"}
    batches = [sorted(batch) for batch in pr_batches(branches)]
    logger.info("Checking GitHub PR state for %d branches in %d batches", len(branches), len(batches))
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as client, asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(_fetch_batch(client, semaphore, owner, name, batch, index=index, total=len(batches)))
                for index, batch in enumerate(batches, start=1)
            ]
    except Exception:
        logger.warning("PR check skipped: GitHub API error", exc_info=True)
        return PrLookup({}, branches)
    states: dict[str, PrInfo] = {}
    unknown: set[str] = set()
    exhausted = 0
    for batch, task in zip(batches, tasks, strict=True):
        result = task.result()
        if isinstance(result, _QuotaExhausted):
            exhausted += 1
            unknown.update(batch)
        else:
            states.update(result.states)
            unknown.update(result.unknown)
    if exhausted:
        logger.warning(
            "PR check incomplete: GitHub GraphQL quota exhausted for %d of %d batches; found PRs for %d of %d branches",
            exhausted,
            len(batches),
            len(states),
            len(branches),
        )
    else:
        logger.info("GitHub PR state complete: %d/%d branches resolved", len(states), len(branches))
    # A partial merged/closed answer is not enough to justify a decision when another PR
    # page may contain an open PR.
    for branch in unknown:
        states.pop(branch, None)
    return PrLookup(states, unknown)

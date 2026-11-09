"""GitHub pull-request state for local branches, fetched with batched GraphQL queries.

`workspace_gc` injects the result into the offline classifiers as `dict[str, PrInfo]`.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
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
_GRAPHQL_STATE = {"MERGED": PrState.MERGED, "OPEN": PrState.OPEN, "CLOSED": PrState.CLOSED}


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
        info = PrInfo(number=node["number"], state=_GRAPHQL_STATE[node["state"]], head_sha=node["headRefOid"])
        if best is None or _PR_RANK[info.state] > _PR_RANK[best.state]:
            best = info
    return best


def pr_states(repo: Path, branches: set[str]) -> dict[str, PrInfo]:
    """Most-decisive PR (merged > open > closed) per branch, via one batched GraphQL query.

    Aliased `pullRequests(headRefName:)` fields fetch ~50 branches per request instead of a
    REST call per branch — a few round-trips rather than hundreds. `headRefName` matches PR
    records, so a branch whose remote ref was deleted after merge still resolves. Returns {}
    when there is no token, no GitHub remote, or the API is unreachable, and returns whatever
    it resolved before a mid-sweep quota exhaustion — the caller classifies the rest on git
    signals alone.
    """
    if not branches:
        return {}
    slug = _repo_slug(repo)
    token = _github_token()
    if slug is None:
        logger.warning("PR check skipped: origin is not a GitHub remote")
        return {}
    if token is None:
        logger.warning("PR check skipped: no GITHUB_TOKEN and `gh auth token` unavailable")
        return {}
    owner, name = slug.split("/", 1)
    headers = {"Authorization": f"bearer {token}", "User-Agent": "workspace-gc"}
    states: dict[str, PrInfo] = {}
    batches = list(itertools.batched(sorted(branches), _GRAPHQL_BATCH, strict=False))
    logger.info("Checking GitHub PR state for %d branches in %d batches", len(branches), len(batches))
    try:
        with httpx.Client(headers=headers, timeout=30) as client:
            for index, batch in enumerate(batches, start=1):
                logger.info("Checking GitHub PR state batch %d/%d (%d branches)", index, len(batches), len(batch))
                query, alias_to_branch = _pr_query(owner, name, list(batch))
                payload = client.post(_GRAPHQL_URL, json={"query": query}).raise_for_status().json()
                data = payload.get("data")
                if not data or data.get("repository") is None:
                    errors = payload.get("errors") or []
                    if any(error.get("type") == "RATE_LIMIT" for error in errors):
                        # Quota is per-hour and shared with every other GitHub caller, so it
                        # says nothing about this repo: keep the batches already resolved,
                        # stop asking, and let the rest classify on git signals alone.
                        logger.warning(
                            "PR check incomplete: GitHub GraphQL quota exhausted; "
                            "classified %d of %d branches with PR data",
                            len(states),
                            len(branches),
                        )
                        return states
                    raise RuntimeError(f"GraphQL returned no data: {errors}")
                repository = data["repository"]
                for alias, branch in alias_to_branch.items():
                    connection = repository.get(alias)
                    if connection is not None and (info := _most_decisive(connection["nodes"])) is not None:
                        states[branch] = info
                logger.info("Finished GitHub PR state batch %d/%d", index, len(batches))
        logger.info("GitHub PR state complete: %d/%d branches resolved", len(states), len(branches))
        return states
    except Exception:
        logger.warning("PR check skipped: GitHub API error", exc_info=True)
        return {}

"""Positive confirmation that a GitHub repository is public, by a plain unauthenticated REST call.

A policy admitting "any public repository" must not infer public from the absence of a
restriction: the GitHub MCP connections behind it authenticate with the operator's own token,
which reaches every private repository the operator can see. An unauthenticated
`GET /repos/{owner}/{repo}` 404s on any repository the anonymous caller cannot see, private or
nonexistent alike, so this check structurally cannot observe, let alone leak, anything from a
private repository: there is no credential behind it that could ever see one.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

API_BASE_URL = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 10.0
# Visibility is stable in the common case, and the unauthenticated GitHub REST API is rate-limited
# to 60 requests/hour total — a day bounds staleness (a repo flipping public/private takes up to
# this long to reflect) without spending that shared budget on every repeat check.
CACHE_TTL_SECONDS = 86400.0


class RepositoryVisibilityUnavailableError(RuntimeError):
    """The check could not positively confirm or refute visibility; callers fail closed."""


async def _fetch_is_public(http_client: httpx.AsyncClient, *, owner: str, repository: str) -> bool:
    try:
        response = await http_client.get(
            f"/repos/{owner}/{repository}", headers={"Accept": "application/vnd.github+json"}
        )
    except httpx.HTTPError as error:
        raise RepositoryVisibilityUnavailableError(
            f"GitHub repository visibility request failed for {owner}/{repository}"
        ) from error
    if response.status_code == 404:
        # Unauthenticated GitHub 404s a private repo exactly like a nonexistent one -- both are
        # "not visible to the public", which is the only question this check answers.
        return False
    if response.status_code != 200:
        raise RepositoryVisibilityUnavailableError(
            f"GitHub returned {response.status_code} checking {owner}/{repository} visibility"
        )
    # Belt-and-suspenders: an unauthenticated 200 should never carry private=true, but this check
    # exists specifically not to trust inference, so confirm the field rather than the status code
    # alone.
    if response.json().get("private") is not False:
        raise RepositoryVisibilityUnavailableError(
            f"GitHub's response for {owner}/{repository} did not confirm private=false"
        )
    return True


@dataclass(frozen=True, slots=True)
class _CachedVisibility:
    is_public: bool
    expires_at: float


class RepositoryVisibilityService:
    """Per-replica TTL cache with single-flight over the unauthenticated lookup.

    Only a successful check (public or confirmed-not-public) is cached; a failure propagates
    uncached so a transient outage does not wedge a repository as unknown for the full TTL.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None, *, ttl_seconds: float = CACHE_TTL_SECONDS) -> None:
        self._http_client = http_client or httpx.AsyncClient(base_url=API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        self._ttl_seconds = ttl_seconds
        self._cache: dict[tuple[str, str], _CachedVisibility] = {}
        self._in_flight: dict[tuple[str, str], asyncio.Task[bool]] = {}

    async def is_public(self, *, owner: str, repository: str) -> bool:
        key = (owner.casefold(), repository.casefold())
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > time.monotonic():
            return cached.is_public
        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._load(key))
            self._in_flight[key] = task
        # Shielded so one caller giving up does not cancel the check every other caller awaits.
        return await asyncio.shield(task)

    async def _load(self, key: tuple[str, str]) -> bool:
        try:
            owner, repository = key
            is_public = await _fetch_is_public(self._http_client, owner=owner, repository=repository)
            self._prune()
            self._cache[key] = _CachedVisibility(is_public=is_public, expires_at=time.monotonic() + self._ttl_seconds)
            return is_public
        finally:
            self._in_flight.pop(key, None)

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [key for key, entry in self._cache.items() if entry.expires_at <= now]:
            del self._cache[key]

    async def aclose(self) -> None:
        await self._http_client.aclose()

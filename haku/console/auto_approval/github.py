"""GitHub-repository auto-approval evaluation: fixed allowlist and confirmed-public repositories.

Both policy kinds resist the same smuggling risk: a caller supplying an approved or confirmed-
public owner/repo alongside a search query that names a *different* repository via its own
`repo:` qualifier, which GitHub Search prefers over the separate owner/repo arguments. The target
repository is therefore always derived the same way before either policy decides what to do with
it.

The public-repo policy must not infer "public" from the absence of a restriction -- haku-console's
``github`` MCP connection authenticates with the operator's own OAuth token, which reaches both
public and private repositories the operator can see, including ``agentydragon/gaffer-private``.
``GitHubRepositoryVisibilityService`` positively confirms visibility instead, with a plain
**unauthenticated** GitHub REST call. An unauthenticated request 404s on any repository the
anonymous caller cannot see -- private or nonexistent alike -- so this check structurally cannot
observe, let alone leak, anything from a private repository: there is no credential behind it that
could ever see one.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from haku.console.auto_approval.decision import AutoApprovalDecision, AutoApproved, NotAutoApproved

logger = logging.getLogger(__name__)

# GitHub MCP's search_pull_requests adds ``repo:<owner>/<repo>`` from the separate owner/repo
# arguments only when the query contains no repository qualifier. Do not allow a caller to bypass
# a repository policy by supplying an approved owner/repo pair alongside a query for another repo.
_SEARCH_REPOSITORY_QUALIFIER = re.compile(r"(?:^|[^\w])repo:", re.IGNORECASE)
# search_code has no separate owner/repo parameters. Its one repository boundary must therefore be
# an unquoted, whitespace-delimited ``repo:owner/repo`` search qualifier. Keep the recognizer
# deliberately narrow: a syntax GitHub may instead interpret as free text cannot establish standing
# authority.
_CODE_SEARCH_REPOSITORY_QUALIFIER = re.compile(r"(?<!\S)repo:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?=$|\s)", re.IGNORECASE)

_GITHUB_API_BASE_URL = "https://api.github.com"
_REQUEST_TIMEOUT_SECONDS = 10.0
# Visibility is stable in the common case, and the unauthenticated GitHub REST API is rate-limited
# to 60 requests/hour total — a day bounds staleness (a repo flipping public/private takes up to
# this long to reflect) without spending that shared budget on every repeat check.
_CACHE_TTL_SECONDS = 86400.0


class GitHubRepositoryVisibilityUnavailableError(RuntimeError):
    """The check could not positively confirm or refute visibility; callers fail closed."""


async def _fetch_is_public(http_client: httpx.AsyncClient, *, owner: str, repository: str) -> bool:
    """Confirm visibility with an unauthenticated ``GET /repos/{owner}/{repo}``."""
    try:
        response = await http_client.get(
            f"/repos/{owner}/{repository}", headers={"Accept": "application/vnd.github+json"}
        )
    except httpx.HTTPError as error:
        raise GitHubRepositoryVisibilityUnavailableError(
            f"GitHub repository visibility request failed for {owner}/{repository}"
        ) from error
    if response.status_code == 404:
        # Unauthenticated GitHub 404s a private repo exactly like a nonexistent one -- both are
        # "not visible to the public", which is the only question this check answers.
        return False
    if response.status_code != 200:
        raise GitHubRepositoryVisibilityUnavailableError(
            f"GitHub returned {response.status_code} checking {owner}/{repository} visibility"
        )
    # Belt-and-suspenders: an unauthenticated 200 should never carry private=true, but this check
    # exists specifically not to trust inference, so confirm the field rather than the status code
    # alone.
    if response.json().get("private") is not False:
        raise GitHubRepositoryVisibilityUnavailableError(
            f"GitHub's response for {owner}/{repository} did not confirm private=false"
        )
    return True


@dataclass(frozen=True, slots=True)
class _CachedVisibility:
    is_public: bool
    expires_at: float


class GitHubRepositoryVisibilityService:
    """Per-replica TTL cache with single-flight, mirroring ``reflection_cache.ReflectionCache``.

    Only a successful check (public or confirmed-not-public) is cached; a failure propagates
    uncached so a transient outage does not wedge a repository as unknown for the full TTL.
    """

    def __init__(
        self, http_client: httpx.AsyncClient | None = None, *, ttl_seconds: float = _CACHE_TTL_SECONDS
    ) -> None:
        self._http_client = http_client or httpx.AsyncClient(
            base_url=_GITHUB_API_BASE_URL, timeout=_REQUEST_TIMEOUT_SECONDS
        )
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


@dataclass(frozen=True, slots=True)
class _TargetRepository:
    """The one repository a call unambiguously targets."""

    owner: str
    repository: str


def _extract_target_repository(tool_name: str, arguments: dict[str, Any]) -> _TargetRepository | NotAutoApproved:
    """Derive the single repository a call targets, shared by the fixed-repo and public-repo policies."""
    if tool_name == "search_code":
        return _extract_code_search_target_repository(arguments)

    actual_owner = arguments.get("owner")
    actual_repository = arguments.get("repo")
    if not isinstance(actual_owner, str) or not isinstance(actual_repository, str):
        return NotAutoApproved("call does not identify a repository with string owner/repo arguments")
    if tool_name == "search_pull_requests":
        query = arguments.get("query")
        if not isinstance(query, str):
            return NotAutoApproved("pull-request search requires a string query")
        if _SEARCH_REPOSITORY_QUALIFIER.search(query):
            return NotAutoApproved(
                "pull-request search query sets a repository qualifier; omit it so owner/repo scopes the search"
            )
    return _TargetRepository(owner=actual_owner, repository=actual_repository)


def _extract_code_search_target_repository(arguments: dict[str, Any]) -> _TargetRepository | NotAutoApproved:
    query = arguments.get("query")
    if not isinstance(query, str):
        return NotAutoApproved("code search requires a string query")

    # Every syntactic occurrence of `repo:` must be the single, deliberately narrow qualifier
    # below. This rejects quoted, negated, duplicate, and malformed qualifiers rather than
    # guessing how GitHub Search will interpret them.
    if len(_SEARCH_REPOSITORY_QUALIFIER.findall(query)) != 1:
        return NotAutoApproved("code search requires exactly one repository qualifier")
    matches = _CODE_SEARCH_REPOSITORY_QUALIFIER.findall(query)
    if len(matches) != 1:
        return NotAutoApproved("code search requires one unquoted repo:owner/repo qualifier")

    owner, _, repository = matches[0].partition("/")
    return _TargetRepository(owner=owner, repository=repository)


def evaluate_fixed_repository(
    tool_name: str, arguments: dict[str, Any], owner: str, repository: str
) -> AutoApprovalDecision:
    """``GitHubRepositoryAutoApprovalPolicy``: the target must match one deploy-configured pair."""
    target = _extract_target_repository(tool_name, arguments)
    if isinstance(target, NotAutoApproved):
        return target
    if (target.owner.casefold(), target.repository.casefold()) != (owner.casefold(), repository.casefold()):
        return NotAutoApproved(f"repository {target.owner}/{target.repository} is outside {owner}/{repository}")
    if tool_name == "search_code":
        return AutoApproved(f"reviewed code search targets repository {owner}/{repository}")
    return AutoApproved(f"reviewed read targets repository {owner}/{repository}")


async def evaluate_public_repository(
    tool_name: str, arguments: dict[str, Any], visibility: GitHubRepositoryVisibilityService | None
) -> AutoApprovalDecision:
    """``GitHubPublicRepositoryAutoApprovalPolicy``: the target must be confirmed public, live."""
    target = _extract_target_repository(tool_name, arguments)
    if isinstance(target, NotAutoApproved):
        return target
    if visibility is None:
        return NotAutoApproved("GitHub repository visibility checking is not configured")
    try:
        is_public = await visibility.is_public(owner=target.owner, repository=target.repository)
    except GitHubRepositoryVisibilityUnavailableError:
        logger.warning(
            "GitHub repository visibility check unavailable owner=%s repository=%s", target.owner, target.repository
        )
        return NotAutoApproved(f"could not confirm {target.owner}/{target.repository} is public")
    if not is_public:
        return NotAutoApproved(f"repository {target.owner}/{target.repository} is not confirmed public")
    if tool_name == "search_code":
        return AutoApproved(
            f"reviewed code search targets confirmed-public repository {target.owner}/{target.repository}"
        )
    return AutoApproved(f"reviewed read targets confirmed-public repository {target.owner}/{target.repository}")

"""The one repository a GitHub MCP tool call targets, and the two policies that decide over it.

Both policies resist the same smuggling risk: a caller supplying an approved or confirmed-public
owner/repo alongside a search query that names a *different* repository via its own `repo:`
qualifier, which GitHub Search prefers over the separate owner/repo arguments. The target
repository is therefore always derived the same way before either policy decides what to do with
it. Tool names are the upstream GitHub MCP server's, the same behind every consumer here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from github_policy.visibility import RepositoryVisibilityService, RepositoryVisibilityUnavailableError

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


@dataclass(frozen=True, slots=True)
class TargetRepository:
    """The one repository a call unambiguously targets, as the call spells it."""

    owner: str
    repository: str

    def names(self, owner: str, repository: str) -> bool:
        """GitHub owner and repository names are case-insensitive."""
        return (self.owner.casefold(), self.repository.casefold()) == (owner.casefold(), repository.casefold())


@dataclass(frozen=True, slots=True)
class RepositoryMatch:
    target: TargetRepository
    confirmed_public: bool
    explanation: str


@dataclass(frozen=True, slots=True)
class RepositoryMismatch:
    reason: str


def _target_repository(tool_name: str, arguments: Mapping[str, object]) -> TargetRepository | RepositoryMismatch:
    if tool_name == "search_code":
        return _code_search_target_repository(arguments)

    actual_owner = arguments.get("owner")
    actual_repository = arguments.get("repo")
    if not isinstance(actual_owner, str) or not isinstance(actual_repository, str):
        return RepositoryMismatch("call does not identify a repository with string owner/repo arguments")
    if tool_name == "search_pull_requests":
        query = arguments.get("query")
        if not isinstance(query, str):
            return RepositoryMismatch("pull-request search requires a string query")
        if _SEARCH_REPOSITORY_QUALIFIER.search(query):
            return RepositoryMismatch(
                "pull-request search query sets a repository qualifier; omit it so owner/repo scopes the search"
            )
    return TargetRepository(owner=actual_owner, repository=actual_repository)


def _code_search_target_repository(arguments: Mapping[str, object]) -> TargetRepository | RepositoryMismatch:
    query = arguments.get("query")
    if not isinstance(query, str):
        return RepositoryMismatch("code search requires a string query")

    # Every syntactic occurrence of `repo:` must be the single, deliberately narrow qualifier
    # below. This rejects quoted, negated, duplicate, and malformed qualifiers rather than
    # guessing how GitHub Search will interpret them.
    if len(_SEARCH_REPOSITORY_QUALIFIER.findall(query)) != 1:
        return RepositoryMismatch("code search requires exactly one repository qualifier")
    matches = _CODE_SEARCH_REPOSITORY_QUALIFIER.findall(query)
    if len(matches) != 1:
        return RepositoryMismatch("code search requires one unquoted repo:owner/repo qualifier")

    owner, _, repository = matches[0].partition("/")
    return TargetRepository(owner=owner, repository=repository)


def _read_kind(tool_name: str) -> str:
    return "code search" if tool_name == "search_code" else "read"


def evaluate_fixed_repository(
    tool_name: str, arguments: Mapping[str, object], owner: str, repository: str
) -> RepositoryMatch | RepositoryMismatch:
    """The target must be the one configured pair."""
    target = _target_repository(tool_name, arguments)
    if isinstance(target, RepositoryMismatch):
        return target
    if not target.names(owner, repository):
        return RepositoryMismatch(f"repository {target.owner}/{target.repository} is outside {owner}/{repository}")
    return RepositoryMatch(
        target=target,
        confirmed_public=False,
        explanation=f"reviewed {_read_kind(tool_name)} targets repository {owner}/{repository}",
    )


async def evaluate_public_repository(
    tool_name: str, arguments: Mapping[str, object], visibility: RepositoryVisibilityService
) -> RepositoryMatch | RepositoryMismatch:
    """The target must be confirmed public, live; an unavailable check is a mismatch, never a match."""
    target = _target_repository(tool_name, arguments)
    if isinstance(target, RepositoryMismatch):
        return target
    try:
        is_public = await visibility.is_public(owner=target.owner, repository=target.repository)
    except RepositoryVisibilityUnavailableError:
        logger.warning(
            "GitHub repository visibility check unavailable owner=%s repository=%s", target.owner, target.repository
        )
        return RepositoryMismatch(f"could not confirm {target.owner}/{target.repository} is public")
    if not is_public:
        return RepositoryMismatch(f"repository {target.owner}/{target.repository} is not confirmed public")
    return RepositoryMatch(
        target=target,
        confirmed_public=True,
        explanation=(
            f"reviewed {_read_kind(tool_name)} targets confirmed-public repository {target.owner}/{target.repository}"
        ),
    )

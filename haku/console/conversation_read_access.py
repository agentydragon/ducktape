"""The profile-DAG read authorizer for conversation history — direct reads and Recall alike.

A conversation pins one `access_profile_id` at creation (#4431). Whether a caller may read that
conversation — through the `haku_conversations` drilldown tools or as a semantic `haku_index` hit —
is decided here, once, from the deploy-reviewed acyclic `can_read_profiles` graph: a profile reads
itself implicitly, plus every profile transitively reachable through its `can_read_profiles` edges.
The graph grants information visibility only; tool authority, approvals, credentials, and runtime
grants never travel along it.

Fail-closed by construction: a caller with no profile, an unknown profile, or a conversation whose
pinned profile is outside the caller's closure reads nothing. Conversations predating pinned
identity (`access_profile_id IS NULL`) are readable only by the browser Operator. The Operator is
trusted with the whole corpus, as everywhere else in the console.
"""

from __future__ import annotations

from dataclasses import dataclass

from haku.console.grants.principal import RequestPrincipal
from haku.console.mcp_config import AccessProfile
from haku.console.mcp_execution import AgentMcpExecutionCaller, McpExecutionCaller, OperatorMcpExecutionCaller
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor


class ConversationAccessDeniedError(Exception):
    """A named conversation or session exists but is outside the caller's readable profiles."""


@dataclass(frozen=True, slots=True)
class UnrestrictedReads:
    """The browser Operator's scope: every conversation, pinned identity or not."""

    def allows(self, access_profile_id: str | None) -> bool:
        return True

    @property
    def profile_filter(self) -> tuple[str, ...] | None:
        """SQL-shaped form: ``None`` means no profile predicate is applied."""
        return None


@dataclass(frozen=True, slots=True)
class ProfileScopedReads:
    """An Agent's scope: conversations pinned to a profile in its transitive read closure."""

    readable_profile_ids: frozenset[str]

    def allows(self, access_profile_id: str | None) -> bool:
        return access_profile_id is not None and access_profile_id in self.readable_profile_ids

    @property
    def profile_filter(self) -> tuple[str, ...] | None:
        return tuple(sorted(self.readable_profile_ids))


type ConversationReadScope = UnrestrictedReads | ProfileScopedReads


class ConversationReadAccessPolicy:
    """Deployment-owned conversation visibility, derived from the access-profile read graph."""

    def __init__(self, profiles: tuple[AccessProfile, ...]) -> None:
        # Transitive closure over `can_read_profiles`, self-read included. Config validation
        # already rejects cycles and unknown references, so a memoized descent terminates.
        edges = {profile.id: profile.can_read_profiles for profile in profiles}
        closures: dict[str, frozenset[str]] = {}

        def closure(profile_id: str) -> frozenset[str]:
            known = closures.get(profile_id)
            if known is None:
                closures[profile_id] = known = frozenset(
                    {profile_id, *(reached for read in edges[profile_id] for reached in closure(read))}
                )
            return known

        self._readable = {profile_id: closure(profile_id) for profile_id in edges}

    def scope_for(self, caller: RuntimeActor | McpExecutionCaller | None) -> ConversationReadScope:
        match caller:
            case OperatorActor() | OperatorMcpExecutionCaller():
                return UnrestrictedReads()
            case (
                AgentActor(access_profile_id=access_profile_id)
                | AgentMcpExecutionCaller(principal=RequestPrincipal(access_profile_id=access_profile_id))
            ):
                pass
            case _:
                return ProfileScopedReads(readable_profile_ids=frozenset())
        if access_profile_id is None:
            return ProfileScopedReads(readable_profile_ids=frozenset())
        return ProfileScopedReads(readable_profile_ids=self._readable.get(access_profile_id, frozenset()))

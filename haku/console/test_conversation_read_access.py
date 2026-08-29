"""The profile-DAG read authorizer: closure, caller mapping, and fail-closed defaults."""

from __future__ import annotations

from uuid import UUID

import pytest_bazel

from haku.console.conversation_read_access import ConversationReadAccessPolicy, ProfileScopedReads, UnrestrictedReads
from haku.console.grants.principal import RequestPrincipal
from haku.console.mcp.execution import AgentMcpExecutionCaller, OperatorMcpExecutionCaller
from haku.console.mcp_config import AccessProfile
from haku.console.tool_call_actor import AgentActor, OperatorActor

_OPERATOR = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _agent(access_profile_id: str | None) -> AgentActor:
    return AgentActor(
        agent_id=UUID(int=1), operator_id=_OPERATOR, binding_id=UUID(int=2), access_profile_id=access_profile_id
    )


def _profile(profile_id: str, *reads: str) -> AccessProfile:
    return AccessProfile(id=profile_id, auto_approval_policy="manual", can_read_profiles=set(reads))


POLICY = ConversationReadAccessPolicy(
    (_profile("haku", "review"), _profile("review", "public-coder"), _profile("public-coder"), _profile("island"))
)


def test_reachability_is_transitive_and_self_read_is_implicit() -> None:
    assert POLICY.scope_for(_agent("haku")) == ProfileScopedReads(
        readable_profile_ids=frozenset({"haku", "review", "public-coder"})
    )
    assert POLICY.scope_for(_agent("public-coder")) == ProfileScopedReads(
        readable_profile_ids=frozenset({"public-coder"})
    )
    assert POLICY.scope_for(_agent("island")) == ProfileScopedReads(readable_profile_ids=frozenset({"island"}))


def test_the_graph_never_reads_upward() -> None:
    scope = POLICY.scope_for(_agent("public-coder"))
    assert not scope.allows("haku")
    assert not scope.allows("review")


def test_unprofiled_unknown_and_absent_callers_fail_closed() -> None:
    for caller in (_agent(None), _agent("retired-profile"), None):
        scope = POLICY.scope_for(caller)
        assert scope == ProfileScopedReads(readable_profile_ids=frozenset())
        assert not scope.allows("haku")


def test_a_pre_identity_conversation_is_readable_only_without_a_profile_fence() -> None:
    """`access_profile_id IS NULL` predates pinned identity and fails closed for every Agent."""
    assert not POLICY.scope_for(_agent("haku")).allows(None)
    assert UnrestrictedReads().allows(None)


def test_operator_callers_read_unrestricted() -> None:
    assert POLICY.scope_for(OperatorActor(operator_id=_OPERATOR)) == UnrestrictedReads()
    assert POLICY.scope_for(OperatorMcpExecutionCaller(operator_id=_OPERATOR)) == UnrestrictedReads()


def test_execution_callers_resolve_through_their_principal() -> None:
    caller = AgentMcpExecutionCaller(
        principal=RequestPrincipal(agent_id=UUID(int=1), session_id=None, access_profile_id="review")
    )
    assert POLICY.scope_for(caller) == ProfileScopedReads(readable_profile_ids=frozenset({"review", "public-coder"}))


def test_the_sql_filter_form_matches_the_scope() -> None:
    assert UnrestrictedReads().profile_filter is None
    assert POLICY.scope_for(_agent("haku")).profile_filter == ("haku", "public-coder", "review")
    assert POLICY.scope_for(_agent(None)).profile_filter == ()


if __name__ == "__main__":
    pytest_bazel.main()

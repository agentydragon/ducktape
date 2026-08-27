"""Contract tests for the configured-index ``haku_index`` MCP surface."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from fastmcp import Client

from haku.console.conversation_read_access import (
    ConversationReadAccessPolicy,
    ConversationReadScope,
    ProfileScopedReads,
    UnrestrictedReads,
)
from haku.console.grant_principal import RequestPrincipal
from haku.console.mcp_config import AccessProfile
from haku.console.mcp_execution import (
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
    mcp_execution_request_meta,
)
from haku.console.recall_index_access import RecallIndexAccessPolicy
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tools.recall_index import (
    HAKU_INDEX_SERVER_ID,
    ChatIndexStatus,
    ChatSource,
    GitIndexStatus,
    GitSource,
    IndexStatus,
    SearchHit,
    SearchResults,
    build_mcp,
)

NOW = datetime.datetime(2026, 8, 14, 9, 0, tzinfo=datetime.UTC)
SESSION = UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = UUID("44444444-4444-4444-4444-444444444444")
MESSAGES = [UUID("22222222-2222-2222-2222-222222222222"), UUID("33333333-3333-3333-3333-333333333333")]
HAKU = AgentActor(
    agent_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    operator_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    binding_id=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
    access_profile_id="haku",
)
CODER = AgentActor(
    agent_id=UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
    operator_id=HAKU.operator_id,
    binding_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
    access_profile_id="public-coder",
)
PROFILES = (
    AccessProfile(
        id="haku",
        auto_approval_policy="manual",
        recall_index_ids={"haku-state", "haku-conversations"},
        can_read_profiles={"public-coder"},
    ),
    AccessProfile(id="public-coder", auto_approval_policy="manual", recall_index_ids={"ducktape-public"}),
)
ACCESS = RecallIndexAccessPolicy(PROFILES, configured_index_ids=("haku-state", "haku-conversations", "ducktape-public"))
READS = ConversationReadAccessPolicy(PROFILES)


def _mcp(searcher: _Searcher):
    return build_mcp(searcher, access=ACCESS, conversation_reads=READS)


def _meta(actor: RuntimeActor = HAKU) -> dict[str, object]:
    caller = (
        AgentMcpExecutionCaller(
            principal=RequestPrincipal(
                agent_id=actor.agent_id, session_id=actor.session_id, access_profile_id=actor.access_profile_id
            )
        )
        if isinstance(actor, AgentActor)
        else OperatorMcpExecutionCaller(operator_id=actor.operator_id)
    )
    return mcp_execution_request_meta(
        McpExecutionContext(caller=caller, tool_call_id="tc_test", approving_operator_id=None, approval_policy_id=None)
    )


async def _call(client: Client, tool: str, arguments: dict, *, actor: RuntimeActor = HAKU, **kwargs):
    return await client.call_tool(tool, arguments, meta=_meta(actor), **kwargs)


def _git_hit(score: float) -> SearchHit:
    return SearchHit(
        score=score,
        content="how to file an intake item",
        source=GitSource(
            index_id="haku-state",
            path="notes/intake.md",
            commit_sha="deadbeef",
            blob_sha="cafe1234",
            byte_start=0,
            byte_end=40,
        ),
    )


def _chat_hit(score: float) -> SearchHit:
    return SearchHit(
        score=score,
        content="user: what about the egress fence",
        source=ChatSource(
            index_id="haku-conversations",
            session_id=SESSION,
            conversation_id=CONVERSATION,
            room_id="!room:allegedly.works",
            message_ids=MESSAGES,
            first_message_at=NOW,
            last_message_at=NOW,
        ),
    )


class _Searcher:
    def __init__(self, *hits: SearchHit, behind: bool = False) -> None:
        self.hits = list(hits)
        self.behind = behind
        self.queries: list[dict] = []
        self.status_queries: list[tuple[str, ...]] = []

    async def search(
        self, query: str, *, index_id: str, limit: int, session_id: UUID | None, scope: ConversationReadScope
    ) -> SearchResults:
        self.queries.append(
            {"query": query, "index_id": index_id, "limit": limit, "session_id": session_id, "scope": scope}
        )
        return SearchResults(hits=self.hits, index=await self.status(index_ids=(index_id,)) if self.behind else None)

    async def status(self, *, index_ids: tuple[str, ...]) -> IndexStatus:
        self.status_queries.append(index_ids)
        statuses: list[GitIndexStatus | ChatIndexStatus] = [
            GitIndexStatus(
                index_id="haku-state",
                indexed_commit="abc123",
                remote_commit="abc123",
                remote_seen_at=NOW,
                branch="main",
                indexed_at=NOW,
                files=12,
                chunks=40,
                embedded_chunks=40,
                pending_chunks=0,
                superseded_chunks=0,
            ),
            ChatIndexStatus(
                index_id="haku-conversations",
                sessions=3,
                chunks=9,
                embedded_chunks=8,
                pending_chunks=1,
                stale_sessions=1,
                unindexed_messages=4,
                lag_seconds=120.0,
                last_indexed_at=NOW,
                superseded_chunks=0,
            ),
        ]
        return IndexStatus(indexes=[status for status in statuses if status.index_id in index_ids])


async def test_a_git_hit_carries_the_index_and_exact_file_pointer() -> None:
    async with Client(_mcp(_Searcher(_git_hit(0.9)))) as client:
        (hit,) = (await _call(client, "search", {"query": "intake", "index_id": "haku-state"})).data.hits
    assert hit.content == "how to file an intake item"
    assert (hit.source["index_id"], hit.source["path"], hit.source["commit_sha"], hit.source["blob_sha"]) == (
        "haku-state",
        "notes/intake.md",
        "deadbeef",
        "cafe1234",
    )


async def test_a_chat_hit_carries_its_index_session_conversation_room_and_messages() -> None:
    async with Client(_mcp(_Searcher(_chat_hit(0.8)))) as client:
        (hit,) = (await _call(client, "search", {"query": "egress fence", "index_id": "haku-conversations"})).data.hits
    assert (hit.source["index_id"], hit.source["session_id"], hit.source["conversation_id"], hit.source["room_id"]) == (
        "haku-conversations",
        str(SESSION),
        str(CONVERSATION),
        "!room:allegedly.works",
    )
    assert hit.source["message_ids"] == [str(message_id) for message_id in MESSAGES]


async def test_content_is_included_by_default_or_explicit_request_and_omitted_on_request() -> None:
    async with Client(_mcp(_Searcher(_git_hit(0.9)))) as client:
        default = await _call(client, "search", {"query": "intake", "index_id": "haku-state"})
        explicit = await _call(client, "search", {"query": "intake", "index_id": "haku-state", "include_content": True})
        pointer_only = await _call(
            client, "search", {"query": "intake", "index_id": "haku-state", "include_content": False}
        )
    assert default.structured_content["hits"][0]["content"] == "how to file an intake item"
    assert explicit.structured_content["hits"][0]["content"] == "how to file an intake item"
    assert "content" not in pointer_only.structured_content["hits"][0]


async def test_search_requires_one_explicit_authorized_index() -> None:
    searcher = _Searcher()
    async with Client(_mcp(searcher)) as client:
        result = await _call(client, "search", {"query": "intake"}, raise_on_error=False)
    assert result.is_error
    assert searcher.queries == []


async def test_authorized_index_and_session_filter_reach_the_searcher() -> None:
    searcher = _Searcher()
    async with Client(_mcp(searcher)) as client:
        await _call(client, "search", {"query": "intake", "index_id": "haku-conversations", "session_id": str(SESSION)})
    query = searcher.queries[-1]
    assert (query["index_id"], query["session_id"]) == ("haku-conversations", SESSION)


async def test_search_rides_on_the_callers_profile_dag_read_scope() -> None:
    """An Agent's chat hits are fenced by the same closure the drilldown applies; the Operator's
    scope is the whole corpus."""
    searcher = _Searcher()
    async with Client(_mcp(searcher)) as client:
        await _call(client, "search", {"query": "intake", "index_id": "haku-conversations"})
        await _call(
            client,
            "search",
            {"query": "public", "index_id": "ducktape-public"},
            actor=OperatorActor(operator_id=HAKU.operator_id),
        )
    agent_query, operator_query = searcher.queries
    assert agent_query["scope"] == ProfileScopedReads(readable_profile_ids=frozenset({"haku", "public-coder"}))
    assert operator_query["scope"] == UnrestrictedReads()


async def test_ungranted_index_fails_before_embedding_or_querying() -> None:
    searcher = _Searcher()
    async with Client(_mcp(searcher)) as client:
        result = await _call(
            client, "search", {"query": "secrets", "index_id": "haku-state"}, actor=CODER, raise_on_error=False
        )
    assert result.is_error
    assert searcher.queries == []


@pytest.mark.parametrize(
    "actor",
    [
        AgentActor(agent_id=UUID(int=1), operator_id=HAKU.operator_id, binding_id=UUID(int=2)),
        AgentActor(
            agent_id=UUID(int=3), operator_id=HAKU.operator_id, binding_id=UUID(int=4), access_profile_id="missing"
        ),
    ],
)
async def test_unprofiled_and_unknown_actors_are_denied_before_querying(actor: RuntimeActor) -> None:
    searcher = _Searcher()
    async with Client(_mcp(searcher)) as client:
        result = await _call(
            client, "search", {"query": "intake", "index_id": "haku-state"}, actor=actor, raise_on_error=False
        )
    assert result.is_error
    assert searcher.queries == []


async def test_a_behind_index_rides_along_with_status() -> None:
    async with Client(_mcp(_Searcher(behind=True))) as client:
        results = (await _call(client, "search", {"query": "intake", "index_id": "haku-conversations"})).data
    assert results.index is not None
    assert results.index.indexes[0]["unindexed_messages"] == 4


async def test_status_reports_only_the_caller_granted_indexes() -> None:
    searcher = _Searcher()
    async with Client(_mcp(searcher)) as client:
        status = (await _call(client, "index_status", {}, actor=CODER)).data
    assert status.indexes == []
    assert searcher.status_queries == [("ducktape-public",)]


async def test_status_denies_an_actor_without_any_recall_grant() -> None:
    searcher = _Searcher()
    actor = AgentActor(agent_id=UUID(int=1), operator_id=HAKU.operator_id, binding_id=UUID(int=2))
    async with Client(_mcp(searcher)) as client:
        result = await _call(client, "index_status", {}, actor=actor, raise_on_error=False)
    assert result.is_error
    assert searcher.status_queries == []


async def test_status_passes_the_full_granted_set_to_the_searcher() -> None:
    searcher = _Searcher()
    async with Client(_mcp(searcher)) as client:
        await _call(client, "index_status", {})
    assert searcher.status_queries == [("haku-conversations", "haku-state")]


async def test_operator_can_search_and_check_status_for_every_configured_index() -> None:
    searcher = _Searcher()
    operator = OperatorActor(operator_id=HAKU.operator_id)
    async with Client(_mcp(searcher)) as client:
        await _call(client, "search", {"query": "public", "index_id": "ducktape-public"}, actor=operator)
        await _call(client, "index_status", {}, actor=operator)
    assert searcher.queries[-1]["index_id"] == "ducktape-public"
    assert searcher.status_queries[-1] == ("ducktape-public", "haku-conversations", "haku-state")


def test_the_server_is_named_for_its_id() -> None:
    assert _mcp(_Searcher()).name == HAKU_INDEX_SERVER_ID


if __name__ == "__main__":
    pytest_bazel.main()

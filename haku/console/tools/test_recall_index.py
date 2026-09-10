"""Contract tests for the configured-index ``haku_index`` MCP surface."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from fastmcp import Client

from haku.console.grants.principal import RequestPrincipal
from haku.console.mcp.execution import (
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
    mcp_execution_request_meta,
)
from haku.console.mcp_config import AccessProfile
from haku.console.recall_index_access import RecallIndexAccessPolicy
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tools.recall_index import (
    HAKU_INDEX_SERVER_ID,
    GitIndexStatus,
    GitSource,
    IndexStatus,
    SearchHit,
    SearchResults,
    build_mcp,
)

NOW = datetime.datetime(2026, 8, 14, 9, 0, tzinfo=datetime.UTC)
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
    AccessProfile(id="haku", auto_approval_policy="manual", recall_index_ids={"haku-state"}),
    AccessProfile(id="public-coder", auto_approval_policy="manual", recall_index_ids={"ducktape-public"}),
)
ACCESS = RecallIndexAccessPolicy(PROFILES, configured_index_ids=("haku-state", "ducktape-public"))


def _mcp(searcher: _Searcher):
    return build_mcp(searcher, access=ACCESS)


def _meta(actor: RuntimeActor = HAKU) -> dict[str, object]:
    caller = (
        AgentMcpExecutionCaller(
            principal=RequestPrincipal(agent_id=actor.agent_id, access_profile_id=actor.access_profile_id)
        )
        if isinstance(actor, AgentActor)
        else OperatorMcpExecutionCaller(operator_id=actor.operator_id)
    )
    return mcp_execution_request_meta(
        McpExecutionContext(caller=caller, tool_call_id="tc_test", approving_operator_id=None, approval_policy_id=None)
    )


async def _call(client: Client, tool: str, arguments: dict, *, actor: RuntimeActor = HAKU, **kwargs):
    return await client.call_tool(tool, arguments, meta=_meta(actor), **kwargs)


def _git_hit(score: float, *, index_id: str = "haku-state") -> SearchHit:
    return SearchHit(
        score=score,
        content="how to file an intake item",
        source=GitSource(
            index_id=index_id,
            path="notes/intake.md",
            commit_sha="deadbeef",
            blob_sha="cafe1234",
            byte_start=0,
            byte_end=40,
        ),
    )


class _Searcher:
    def __init__(self, *hits: SearchHit, behind: bool = False) -> None:
        self.hits = list(hits)
        self.behind = behind
        self.queries: list[dict] = []
        self.status_queries: list[tuple[str, ...]] = []

    async def search(self, query: str, *, index_id: str, limit: int) -> SearchResults:
        self.queries.append({"query": query, "index_id": index_id, "limit": limit})
        return SearchResults(hits=self.hits, index=await self.status(index_ids=(index_id,)) if self.behind else None)

    async def status(self, *, index_ids: tuple[str, ...]) -> IndexStatus:
        self.status_queries.append(index_ids)
        statuses: list[GitIndexStatus] = [
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
            GitIndexStatus(
                index_id="ducktape-public",
                indexed_commit="def456",
                remote_commit="def789",
                remote_seen_at=NOW,
                branch="devel",
                indexed_at=NOW,
                files=30,
                chunks=90,
                embedded_chunks=88,
                pending_chunks=2,
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
        results = (await _call(client, "search", {"query": "public", "index_id": "ducktape-public"}, actor=CODER)).data
    assert results.index is not None
    assert results.index.indexes[0]["pending_chunks"] == 2


async def test_status_reports_only_the_caller_granted_indexes() -> None:
    searcher = _Searcher()
    async with Client(_mcp(searcher)) as client:
        status = (await _call(client, "index_status", {}, actor=CODER)).data
    assert [entry["index_id"] for entry in status.indexes] == ["ducktape-public"]
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
    assert searcher.status_queries == [("haku-state",)]


async def test_operator_can_search_and_check_status_for_every_configured_index() -> None:
    searcher = _Searcher()
    operator = OperatorActor(operator_id=HAKU.operator_id)
    async with Client(_mcp(searcher)) as client:
        await _call(client, "search", {"query": "public", "index_id": "ducktape-public"}, actor=operator)
        await _call(client, "index_status", {}, actor=operator)
    assert searcher.queries[-1]["index_id"] == "ducktape-public"
    assert searcher.status_queries[-1] == ("ducktape-public", "haku-state")


def test_the_server_is_named_for_its_id() -> None:
    assert _mcp(_Searcher()).name == HAKU_INDEX_SERVER_ID


if __name__ == "__main__":
    pytest_bazel.main()

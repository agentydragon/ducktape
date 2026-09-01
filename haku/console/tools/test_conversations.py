"""Tests for the in-process `haku_conversations` MCP server (build_mcp)."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastmcp import Client, FastMCP
from fastmcp.client.client import CallToolResult
from more_itertools import one
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conftest import console_sessions, operator_identity_store
from haku.console.conversation.conversation_event import TurnAnswered, TurnOutcome
from haku.console.conversation.item_reads import FromFrames, Item, MessageItem
from haku.console.conversation.item_vocabulary import ItemStatus, ItemType
from haku.console.conversation.reader import ConversationReads
from haku.console.conversation.reads import (
    ChannelAttachment,
    FrameRecord,
    HarnessFrameRecord,
    SessionCursor,
    SessionOutcome,
    SessionRecord,
    TurnCursor,
    TurnRecord,
)
from haku.console.conversation_read_access import (
    ConversationAccessDeniedError,
    ConversationReadAccessPolicy,
    ConversationReadScope,
    ProfileScopedReads,
)
from haku.console.database_schema import Conversation, ConversationItem, ConversationTurn, Session
from haku.console.grants.principal import RequestPrincipal
from haku.console.harnesses.kind import HarnessKind
from haku.console.identity.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.mcp.execution import (
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
    mcp_execution_request_meta,
)
from haku.console.mcp.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp_config import AccessProfile
from haku.console.session.session_frames import SessionFrameKind
from haku.console.session.status import SessionStatus
from haku.console.session.store import Store
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tools.conversations import HAKU_CONVERSATIONS_SERVER_ID, FramePage, ItemPage, SessionPage, build_mcp

SESSION = UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = UUID("44444444-4444-4444-4444-444444444444")
OLDER_SESSION = UUID("33333333-3333-3333-3333-333333333333")
TURN = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime.datetime(2026, 8, 12, 9, 0, tzinfo=datetime.UTC)
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
        in_process_server_ids={"haku_conversations"},
        can_read_profiles={"public-coder"},
    ),
    AccessProfile(id="public-coder", auto_approval_policy="manual"),
)
ACCESS = InProcessServerAccessPolicy(PROFILES)
READS = ConversationReadAccessPolicy(PROFILES)

# Every tool that pages, and how a page of it is asked for. The point of the surface is that this
# list can be walked with one loop, so the tests below walk it.
PAGED_TOOLS: tuple[tuple[str, dict[str, str]], ...] = (
    ("list_sessions", {}),
    ("list_turns", {"session_id": str(SESSION)}),
    ("read_conversation_items", {"conversation_id": str(CONVERSATION)}),
    ("read_session_frames", {"session_id": str(SESSION)}),
)


def _items(result: CallToolResult) -> ItemPage:
    """The page as its own declared model, which also checks that the wire round-trips into it.

    `result.data` reconstructs a page from the generated schema and leaves a discriminated union's
    members as plain dicts, so an item read off it is untyped either way.
    """
    return ItemPage.model_validate(result.structured_content)


def _frames(result: CallToolResult) -> FramePage:
    """The page as its own declared model — `result.data` leaves union members as plain dicts."""
    return FramePage.model_validate(result.structured_content)


def _session(session_id: UUID, created_at: datetime.datetime) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        conversation_id=CONVERSATION,
        harness_kind=HarnessKind.CLAUDE_CODE,
        attachments=[ChannelAttachment(surface="matrix", address="!room:example.org", attached_at=created_at)],
        status="closed",
        created_at=created_at,
    )


def _frame(seq: int, kind: str = "assistant", payload: dict | None = None) -> HarnessFrameRecord:
    return HarnessFrameRecord(
        frame_seq=seq,
        direction="from_agent",
        created_at=NOW,
        payload=payload if payload is not None else {"type": kind},
    )


def _message(seq: int, *, first_frame_seq: int, last_frame_seq: int | None = None) -> MessageItem:
    return MessageItem(
        opened_seq=seq,
        closed_seq=seq + 1,
        status=ItemStatus.COMPLETE,
        provenance=FromFrames(
            session_id=SESSION, first_frame_seq=first_frame_seq, last_frame_seq=last_frame_seq or first_frame_seq
        ),
        text=f"answer {seq}",
        backend_item_id=f"msg_{seq}",
    )


class _Reader:
    """A `ConversationReader` over lists, recording how it was queried."""

    def __init__(self, *frames: HarnessFrameRecord, items: Sequence[Item] = (), denies: bool = False):
        self._frames: list[FrameRecord] = list(frames)
        self._items = list(items)
        self._denies = denies
        self.queries: list[dict] = []
        self.session_cursors: list[SessionCursor | None] = []
        self.scopes: list[ConversationReadScope] = []
        # Newest first, the order the store lists them in.
        self._sessions = [_session(SESSION, NOW), _session(OLDER_SESSION, NOW - datetime.timedelta(hours=1))]

    def _point_read(self, scope: ConversationReadScope) -> None:
        self.scopes.append(scope)
        if self._denies:
            raise ConversationAccessDeniedError("out of scope")

    async def list_sessions(
        self, *, cursor: SessionCursor | None, limit: int, scope: ConversationReadScope
    ) -> list[SessionRecord]:
        self.scopes.append(scope)
        self.session_cursors.append(cursor)
        return [
            session
            for session in self._sessions
            if cursor is None or (session.created_at, session.session_id) <= (cursor.created_at, cursor.session_id)
        ][:limit]

    async def read_session_frames(
        self,
        session_id: UUID,
        *,
        cursor: int | None,
        limit: int,
        scope: ConversationReadScope,
        kinds: Sequence[SessionFrameKind] | None = None,
    ) -> list[FrameRecord]:
        self._point_read(scope)
        self.queries.append({"session_id": session_id, "cursor": cursor, "limit": limit, "kinds": kinds})
        selected = [frame for frame in self._frames if kinds is None or frame.kind in kinds]
        if cursor is not None:
            selected = [frame for frame in selected if frame.frame_seq >= cursor]
        return selected[:limit]

    async def list_turns(
        self, session_id: UUID, *, cursor: TurnCursor | None, limit: int, scope: ConversationReadScope
    ) -> list[TurnRecord]:
        self._point_read(scope)
        self.queries.append({"session_id": session_id, "cursor": cursor, "limit": limit})
        return [
            TurnRecord(
                turn_id=TURN, first_frame_seq=1, last_frame_seq=4, started_at=NOW, ended_at=NOW, end=TurnAnswered()
            )
        ][:limit]

    async def read_conversation_items(
        self, conversation_id: UUID, *, cursor: int | None, limit: int, scope: ConversationReadScope
    ) -> list[Item]:
        self._point_read(scope)
        self.queries.append({"conversation_id": conversation_id, "cursor": cursor, "limit": limit})
        selected = self._items if cursor is None else [item for item in self._items if item.opened_seq >= cursor]
        return selected[:limit]

    async def session_outcome(self, session_id: UUID, *, scope: ConversationReadScope) -> SessionOutcome:
        self._point_read(scope)
        self.queries.append({"session_id": session_id})
        return SessionOutcome(status=SessionStatus.READY, error=None, latest_turn=None, final_message=None)


def _mcp(reader: _Reader):
    return build_mcp(reader, access=ACCESS, conversation_reads=READS)


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


async def test_tool_surface() -> None:
    async with Client(_mcp(_Reader())) as client:
        tools = {tool.name for tool in await client.list_tools()}

    assert tools == {"list_sessions", "list_turns", "read_conversation_items", "read_session_frames", "session_outcome"}


async def test_harness_kind_is_a_required_closed_identity_field_on_a_session() -> None:
    # harness_kind identity field (naming_and_layout.md §3.1, #4772 C4d): a session publishes
    # `harness_kind` — a required carrier of the closed harness enum, whose component is now
    # `HarnessKind` (the C4d phase-2 stored+published rename; wire values are unchanged).
    async with Client(_mcp(_Reader())) as client:
        list_sessions = one(tool for tool in await client.list_tools() if tool.name == "list_sessions")

    schema = list_sessions.outputSchema
    assert schema is not None
    session = schema["properties"]["items"]["items"]  # FastMCP serves the page schema fully dereferenced.
    assert session["properties"]["harness_kind"]["enum"] == ["claude_code", "codex_app_server"]
    assert "harness_kind" in session["required"]
    assert "runtime_kind" not in session["properties"]


async def test_ungranted_actor_cannot_read_conversations() -> None:
    reader = _Reader()
    async with Client(_mcp(reader)) as client:
        result = await _call(client, "list_sessions", {}, actor=CODER, raise_on_error=False)
    assert result.is_error
    assert reader.session_cursors == []


@pytest.mark.parametrize(
    "actor",
    [
        AgentActor(agent_id=UUID(int=1), operator_id=HAKU.operator_id, binding_id=UUID(int=2)),
        AgentActor(
            agent_id=UUID(int=3), operator_id=HAKU.operator_id, binding_id=UUID(int=4), access_profile_id="missing"
        ),
        OperatorActor(operator_id=HAKU.operator_id),
    ],
)
async def test_unprofiled_unknown_and_operator_actors_cannot_read_conversations(actor: RuntimeActor) -> None:
    reader = _Reader()
    async with Client(_mcp(reader)) as client:
        result = await _call(client, "list_sessions", {}, actor=actor, raise_on_error=False)
    assert result.is_error
    assert reader.session_cursors == []


async def test_every_read_carries_the_callers_profile_dag_scope() -> None:
    """The row fence rides on every read: the caller's profile plus its transitive
    `can_read_profiles` closure, computed by the one authorizer Recall also uses."""
    reader = _Reader(_frame(1), items=[_message(2, first_frame_seq=1)])

    async with Client(_mcp(reader)) as client:
        for tool, arguments in PAGED_TOOLS:
            await _call(client, tool, arguments)

    assert reader.scopes == [ProfileScopedReads(readable_profile_ids=frozenset({"haku", "public-coder"}))] * len(
        PAGED_TOOLS
    )


async def test_a_read_outside_the_scope_is_refused_as_denied() -> None:
    """Loud, not empty: a denied session must not read as an exhausted or absent one."""
    reader = _Reader(_frame(1), items=[_message(2, first_frame_seq=1)], denies=True)

    async with Client(_mcp(reader)) as client:
        for tool, arguments in PAGED_TOOLS[1:]:  # every point read; the listing filters instead
            result = await _call(client, tool, arguments, raise_on_error=False)
            assert result.is_error, tool
            assert "conversation access denied" in str(result.content), tool


async def test_every_listing_answers_in_the_same_shape() -> None:
    """The point of the surface: one loop reads any of them. A tool that grew its own envelope
    would pass its own test and still break that."""
    reader = _Reader(_frame(1), items=[_message(2, first_frame_seq=1)])

    async with Client(_mcp(reader)) as client:
        for tool, arguments in PAGED_TOOLS:
            result = await _call(client, tool, arguments)
            assert result.data.items, tool
            assert hasattr(result.data, "next_cursor"), tool


async def test_a_session_names_its_thread_and_the_channels_holding_a_copy_of_it() -> None:
    """The thread rather than a surface enum: what a reader groups sessions by, what keys
    `read_conversation_items`, and what tells it where the same conversation is also being read."""
    async with Client(_mcp(_Reader())) as client:
        result = await _call(client, "list_sessions", {})

    assert not result.is_error
    page = SessionPage.model_validate(result.structured_content)
    assert page.items[0].conversation_id == CONVERSATION
    assert page.items[0].harness_kind == "claude_code"
    assert [attachment.address for attachment in page.items[0].attachments] == ["!room:example.org"]


async def test_a_full_page_of_sessions_names_both_halves_of_the_key_in_its_cursor() -> None:
    """`created_at` alone does not order the corpus — two sessions can start in one instant — so
    the cursor has to carry the tiebreak rather than pretend one column suffices."""
    async with Client(_mcp(_Reader())) as client:
        result = await _call(client, "list_sessions", {"limit": 1})

    page = SessionPage.model_validate(result.structured_content)
    assert [session.session_id for session in page.items] == [SESSION]
    assert page.next_cursor == SessionCursor(created_at=NOW - datetime.timedelta(hours=1), session_id=OLDER_SESSION)


async def test_the_session_cursor_reaches_the_store_and_the_last_page_offers_none() -> None:
    """Paging belongs in the query: filtering a page here would return fewer rows than asked for
    and read as the end of the corpus."""
    reader = _Reader()
    cursor = SessionCursor.of(_session(OLDER_SESSION, NOW - datetime.timedelta(hours=1)))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "list_sessions", {"limit": 1, "cursor": cursor.model_dump(mode="json")})

    assert reader.session_cursors == [cursor]
    page = SessionPage.model_validate(result.structured_content)
    assert [session.session_id for session in page.items] == [OLDER_SESSION]
    assert page.next_cursor is None


async def test_a_cursor_names_the_first_row_the_page_did_not_return() -> None:
    """Not the last row it did — so it is a position a caller can also arrive at from elsewhere,
    which is what makes an item's `first_frame_seq` a cursor as it stands."""
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3, 4)))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_session_frames", {"session_id": str(SESSION), "limit": 2})

    page = _frames(result)
    assert [frame.frame_seq for frame in page.items] == [1, 2]
    assert page.next_cursor == 3


async def test_a_short_page_is_the_last_one() -> None:
    """Otherwise a reader pages forever, asking for rows that do not exist."""
    reader = _Reader(_frame(1), _frame(2))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_session_frames", {"session_id": str(SESSION), "limit": 25})

    assert _frames(result).next_cursor is None


async def test_frames_return_discriminator_free_native_json_unchanged() -> None:
    native = {"阶段": "最终", "正文": "你好", "成功": True}
    reader = _Reader(_frame(1, payload=native))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_session_frames", {"session_id": str(SESSION)})

    [only] = _frames(result).items
    assert isinstance(only, HarnessFrameRecord)
    assert only.payload == native


async def test_the_cursor_reaches_the_store_rather_than_being_filtered_here() -> None:
    """Paging has to happen in the query; filtering a page after the fact would return
    fewer rows than asked for and read as the end of the log."""
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3)))

    async with Client(_mcp(reader)) as client:
        await _call(
            client, "read_session_frames", {"session_id": str(SESSION), "cursor": 2, "kinds": ["harness_frame"]}
        )

    # 26 rather than 25: the extra row is how the page tells "exactly full" from "more to come".
    assert reader.queries == [
        {"session_id": SESSION, "cursor": 2, "limit": 26, "kinds": [SessionFrameKind.HARNESS_FRAME]}
    ]


async def test_a_one_frame_page_is_the_named_frame_whole() -> None:
    """The exact-frame recipe an item's provenance hands out: cursor at the frame, `limit=1`."""
    big = _frame(1, payload={"type": "user", "content": "x" * 500_000})
    reader = _Reader(big, _frame(2))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_session_frames", {"session_id": str(SESSION), "cursor": 1, "limit": 1})

    page = _frames(result)
    [only] = page.items
    assert isinstance(only, HarnessFrameRecord)
    assert only.payload == big.payload
    assert page.next_cursor == 2


async def test_a_one_frame_page_reads_any_native_json_shape() -> None:
    """Any native JSON shape stays reachable as exact forensic evidence."""
    frame = _frame(
        8,
        kind="codex/event/unknown",
        payload={"jsonrpc": "2.0", "method": "codex/event/unknown", "params": {"opaque": True}},
    )
    reader = _Reader(frame)

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_session_frames", {"session_id": str(SESSION), "cursor": 8, "limit": 1})

    [only] = _frames(result).items
    assert isinstance(only, HarnessFrameRecord)
    assert only.payload == frame.payload


async def test_a_turn_carries_the_range_to_read() -> None:
    """The point of listing exchanges is to pick one and then read its frames, so the bracket is
    what a listing has to come back with."""
    async with Client(_mcp(_Reader())) as client:
        result = await _call(client, "list_turns", {"session_id": str(SESSION)})

    [turn] = result.data.items
    assert (turn.first_frame_seq, turn.last_frame_seq) == (1, 4)
    assert turn.end == {"outcome": "answered"}


async def test_an_item_reads_as_the_conversation_rather_than_the_protocol() -> None:
    """Nothing an MCP caller sees here is `assistant`, a content block or a `tool_use_result`."""
    reader = _Reader(items=[_message(7, first_frame_seq=3, last_frame_seq=5)])

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_conversation_items", {"conversation_id": str(CONVERSATION)})

    item = one(_items(result).items)
    assert isinstance(item, MessageItem)
    assert item.text == "answer 7"
    assert item.provenance == FromFrames(session_id=SESSION, first_frame_seq=3, last_frame_seq=5)


async def test_an_items_provenance_is_a_frame_read_with_no_arithmetic() -> None:
    """The reason provenance exists: appeal a normalization to the frames behind it. It names the
    session because the conversation spans replaced sessions, and an exclusive cursor would need a
    `- 1` here — an off-by-one that reads the wrong frame while looking right."""
    reader = _Reader(_frame(3), _frame(4), items=[_message(7, first_frame_seq=3, last_frame_seq=4)])

    async with Client(_mcp(reader)) as client:
        item = one(_items(await _call(client, "read_conversation_items", {"conversation_id": str(CONVERSATION)})).items)
        assert isinstance(item.provenance, FromFrames)
        span = await _call(
            client,
            "read_session_frames",
            {"session_id": str(item.provenance.session_id), "cursor": item.provenance.first_frame_seq},
        )

    assert [frame.frame_seq for frame in _frames(span).items] == [3, 4]


async def test_an_item_cursor_resumes_where_the_page_stopped() -> None:
    reader = _Reader(items=[_message(seq, first_frame_seq=seq) for seq in (2, 4, 6, 8)])

    async with Client(_mcp(reader)) as client:
        first = await _call(client, "read_conversation_items", {"conversation_id": str(CONVERSATION), "limit": 2})
        second = await _call(
            client, "read_conversation_items", {"conversation_id": str(CONVERSATION), "limit": 2, "cursor": 6}
        )

    assert [item.opened_seq for item in _items(first).items] == [2, 4]
    assert _items(first).next_cursor == 6
    assert [item.opened_seq for item in _items(second).items] == [6, 8]
    assert _items(second).next_cursor is None


async def test_a_page_size_above_the_cap_is_refused() -> None:
    """The cap is the only thing keeping a read from being a dump."""
    async with Client(_mcp(_Reader())) as client:
        result = await _call(
            client, "read_session_frames", {"session_id": str(SESSION), "limit": 10_000}, raise_on_error=False
        )

    assert result.is_error


async def test_a_session_id_that_is_not_an_id_is_refused_here() -> None:
    """The parameter is a `UUID`, so the schema refuses this before any code runs and the store
    is never handed something it would have to validate."""
    reader = _Reader()
    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_session_frames", {"session_id": "not-an-id"}, raise_on_error=False)

    assert result.is_error
    assert reader.queries == []


# The session-outcome contract also belongs to the conversations server. These cases use the
# migrated store and the real profile-DAG read scope, unlike the in-memory surface tests above.
_OUTCOME_ORCHESTRATOR = "haku"
_OUTCOME_WORKER_PROFILE = "public-coder"
_OUTCOME_OUTSIDER = "outsider"
_OUTCOME_PROFILES = (
    AccessProfile(
        id=_OUTCOME_ORCHESTRATOR,
        auto_approval_policy="manual",
        in_process_server_ids={HAKU_CONVERSATIONS_SERVER_ID},
        can_read_profiles={_OUTCOME_WORKER_PROFILE},
    ),
    AccessProfile(id=_OUTCOME_WORKER_PROFILE, auto_approval_policy="manual"),
    AccessProfile(
        id=_OUTCOME_OUTSIDER, auto_approval_policy="manual", in_process_server_ids={HAKU_CONVERSATIONS_SERVER_ID}
    ),
)
_OUTCOME_ACCESS = InProcessServerAccessPolicy(_OUTCOME_PROFILES)
_OUTCOME_READS = ConversationReadAccessPolicy(_OUTCOME_PROFILES)
_OUTCOME_WORKER_AGENT = UUID("40000000-0000-4000-8000-00000000cc01")
_OUTCOME_ORCHESTRATOR_AGENT = UUID("40000000-0000-4000-8000-00000000cc02")
_OUTCOME_FAR_FUTURE = datetime.datetime(2999, 1, 1, tzinfo=datetime.UTC)


def _outcome_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@dataclass(frozen=True, slots=True)
class _OutcomeEnv:
    sessions: async_sessionmaker[AsyncSession]
    store: Store
    mcp: FastMCP
    operator_id: UUID
    worker_agent_id: UUID


@pytest.fixture
async def outcome_env(migrated_db_url: str) -> _OutcomeEnv:
    sessions = console_sessions(migrated_db_url)
    identity_store = operator_identity_store(migrated_db_url)
    operator_id = await identity_store.resolve_configured_external_user_key("worker-op")
    authority = PostgresAgentAuthority(
        sessions,
        public_base_url="https://haku.test",
        operator_identity_store=identity_store,
        access_profiles=(_OUTCOME_ORCHESTRATOR, _OUTCOME_WORKER_PROFILE, _OUTCOME_OUTSIDER),
        default_access_profile_id=_OUTCOME_WORKER_PROFILE,
    )
    await authority.reconcile_static_agents(
        [
            StaticAgentDefinition(
                agent_id=_OUTCOME_WORKER_AGENT,
                display_name="Public Coder",
                operator_id=operator_id,
                secret_reference="env:HAKU_CONSOLE_TEST_WORKER_TOKEN",
                token_fingerprint=fingerprint_static_token("worker-result-token"),
                access_profile_id=_OUTCOME_WORKER_PROFILE,
            )
        ]
    )
    store = Store(sessions)
    return _OutcomeEnv(
        sessions=sessions,
        store=store,
        mcp=build_mcp(ConversationReads(store), access=_OUTCOME_ACCESS, conversation_reads=_OUTCOME_READS),
        operator_id=operator_id,
        worker_agent_id=_OUTCOME_WORKER_AGENT,
    )


async def _outcome_seed_session(
    env: _OutcomeEnv, *, ready: bool, profile: str = _OUTCOME_WORKER_PROFILE
) -> tuple[UUID, UUID]:
    """A worker conversation and its session, `ready` (live) or idle (dispatched, unstarted)."""
    now = _outcome_now()
    conversation_id, session_id = uuid4(), uuid4()
    async with env.sessions.begin() as db:
        db.add(
            Conversation(
                conversation_id=conversation_id,
                operator_id=env.operator_id,
                agent_id=env.worker_agent_id,
                access_profile_id=profile,
                harness_kind=HarnessKind.CODEX_APP_SERVER,
                created_at=now,
                next_event_seq=1,
            )
        )
        await db.flush()
        live = (
            {
                "bridge_token_fingerprint": session_id.bytes,
                "bridge_connected_at": now,
                "lease_expires_at": _OUTCOME_FAR_FUTURE,
            }
            if ready
            else {}
        )
        db.add(
            Session(
                session_id=session_id,
                operator_id=env.operator_id,
                conversation_id=conversation_id,
                created_at=now,
                updated_at=now,
                **live,
            )
        )
    return session_id, conversation_id


async def _outcome_seed_turn(
    env: _OutcomeEnv,
    conversation_id: UUID,
    session_id: UUID,
    *,
    outcome: TurnOutcome | None,
    failure: str | None = None,
) -> UUID:
    now = _outcome_now()
    ended = outcome is not None
    turn_id = uuid4()
    async with env.sessions.begin() as db:
        db.add(
            ConversationTurn(
                turn_id=turn_id,
                conversation_id=conversation_id,
                session_id=session_id,
                first_seq=1,
                last_seq=2 if ended else None,
                first_frame_seq=1,
                last_frame_seq=2 if ended else None,
                started_at=now,
                ended_at=now if ended else None,
                outcome=outcome,
                failure=failure,
            )
        )
    return turn_id


async def _outcome_seed_message(
    env: _OutcomeEnv, conversation_id: UUID, session_id: UUID, text: str, turn_id: UUID | None = None
) -> None:
    now = _outcome_now()
    async with env.sessions.begin() as db:
        db.add(
            ConversationItem(
                item_id=uuid4(),
                conversation_id=conversation_id,
                session_id=session_id,
                turn_id=turn_id,
                item_type=ItemType.MESSAGE,
                status=ItemStatus.COMPLETE,
                opened_seq=3,
                closed_seq=4,
                item_text=text,
                created_at=now,
                updated_at=now,
            )
        )


def _outcome_meta(profile: str) -> dict[str, object]:
    caller = AgentMcpExecutionCaller(
        principal=RequestPrincipal(agent_id=_OUTCOME_ORCHESTRATOR_AGENT, session_id=None, access_profile_id=profile)
    )
    return mcp_execution_request_meta(
        McpExecutionContext(caller=caller, tool_call_id="tc_test", approving_operator_id=None, approval_policy_id=None)
    )


async def _outcome_call(
    env: _OutcomeEnv, session_id: UUID, *, profile: str = _OUTCOME_ORCHESTRATOR, raise_on_error: bool = False
):
    async with Client(env.mcp) as client:
        return await client.call_tool(
            "session_outcome",
            {"session_id": str(session_id)},
            meta=_outcome_meta(profile),
            raise_on_error=raise_on_error,
        )


async def _outcome_result(
    env: _OutcomeEnv, session_id: UUID, *, profile: str = _OUTCOME_ORCHESTRATOR
) -> SessionOutcome:
    return SessionOutcome.model_validate((await _outcome_call(env, session_id, profile=profile)).structured_content)


async def test_a_dispatched_session_with_no_turn_yet_reports_real_status(outcome_env: _OutcomeEnv) -> None:
    session_id, _ = await _outcome_seed_session(outcome_env, ready=False)

    result = await _outcome_result(outcome_env, session_id)

    assert result.status is SessionStatus.IDLE
    assert result.latest_turn is None


async def test_an_open_turn_reports_running_shape(outcome_env: _OutcomeEnv) -> None:
    session_id, conversation_id = await _outcome_seed_session(outcome_env, ready=True)
    await _outcome_seed_turn(outcome_env, conversation_id, session_id, outcome=None)

    result = await _outcome_result(outcome_env, session_id)

    assert result.status is SessionStatus.READY
    assert result.latest_turn is not None
    assert result.latest_turn.ended_at is None
    assert result.latest_turn.end is None


async def test_a_session_with_an_answered_turn_reports_the_final_message(outcome_env: _OutcomeEnv) -> None:
    """An answered turn does not close a still-live one-shot worker session."""
    session_id, conversation_id = await _outcome_seed_session(outcome_env, ready=True)
    turn_id = await _outcome_seed_turn(outcome_env, conversation_id, session_id, outcome=TurnOutcome.ANSWERED)
    await _outcome_seed_message(
        outcome_env, conversation_id, session_id, "Opened the PR: https://example.test/pr/7", turn_id
    )

    assert await outcome_env.store.status(session_id) is SessionStatus.READY
    result = await _outcome_result(outcome_env, session_id)

    assert result.status is SessionStatus.READY
    assert result.latest_turn is not None
    assert result.latest_turn.end is not None
    assert result.latest_turn.end.outcome is TurnOutcome.ANSWERED
    assert result.final_message == "Opened the PR: https://example.test/pr/7"


async def test_an_aborted_turn_reports_aborted_without_a_final_message(outcome_env: _OutcomeEnv) -> None:
    session_id, conversation_id = await _outcome_seed_session(outcome_env, ready=True)
    await _outcome_seed_turn(outcome_env, conversation_id, session_id, outcome=TurnOutcome.ABORTED)

    result = await _outcome_result(outcome_env, session_id)

    assert result.status is SessionStatus.READY
    assert result.latest_turn is not None
    assert result.latest_turn.end is not None
    assert result.latest_turn.end.outcome is TurnOutcome.ABORTED
    assert result.final_message is None


async def test_a_failed_session_carries_its_error(outcome_env: _OutcomeEnv) -> None:
    session_id, _ = await _outcome_seed_session(outcome_env, ready=True)
    await outcome_env.store.fail(session_id, "sandbox runner disconnected")

    result = await _outcome_result(outcome_env, session_id)

    assert result.status is SessionStatus.FAILED
    assert result.error == "sandbox runner disconnected"
    assert result.final_message is None


async def test_a_failed_turn_preserves_its_real_outcome(outcome_env: _OutcomeEnv) -> None:
    session_id, conversation_id = await _outcome_seed_session(outcome_env, ready=True)
    await _outcome_seed_turn(
        outcome_env, conversation_id, session_id, outcome=TurnOutcome.FAILED, failure="the model returned an error"
    )

    result = await _outcome_result(outcome_env, session_id)

    assert result.status is SessionStatus.READY
    assert result.latest_turn is not None
    assert result.latest_turn.end is not None
    assert result.latest_turn.end.outcome is TurnOutcome.FAILED


async def test_a_session_outside_the_read_scope_is_refused(outcome_env: _OutcomeEnv) -> None:
    session_id, conversation_id = await _outcome_seed_session(outcome_env, ready=True)
    turn_id = await _outcome_seed_turn(outcome_env, conversation_id, session_id, outcome=TurnOutcome.ANSWERED)
    await _outcome_seed_message(outcome_env, conversation_id, session_id, "secret", turn_id)

    result = await _outcome_call(outcome_env, session_id, profile=_OUTCOME_OUTSIDER, raise_on_error=False)

    assert result.is_error
    assert "conversation access denied" in str(result.content)


async def test_an_unknown_session_is_refused(outcome_env: _OutcomeEnv) -> None:
    result = await _outcome_call(outcome_env, uuid4(), raise_on_error=False)

    assert result.is_error
    assert "worker session not found" in str(result.content)


if __name__ == "__main__":
    pytest_bazel.main()

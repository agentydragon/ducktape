"""Contracts of the session store: the rows, and which of them commit together.

A room is an address on a `channel_attachment` row here and nothing more — no channel is imported, so
this file is what a second channel inherits (<README.md> § The runtime's conftest names no
channel).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.chat_models import (
    OPEN_SESSION_STATUSES,
    SPA_ORIGIN,
    BridgeFrameKind,
    FrameDirection,
    ItemStatus,
    ItemType,
    LeaseExpiryReason,
    MatrixOrigin,
    PromptOriginKind,
    PromptRejection,
    RuntimeKind,
    SessionStatus,
    SpaOrigin,
    ToolOutcome,
)
from haku.console.conversation.conversation_event import (
    AuthoredEventKind,
    ConversationEventKind,
    EventProvenance,
    FrameRange,
    PromptOpened,
    TurnAborted,
    TurnAnswered,
    TurnFailed,
    TurnOutcome,
)
from haku.console.conversation.item_reads import entry_of
from haku.console.conversation.reads import (
    FrameRecord,
    FromFrames,
    HarnessFrameRecord,
    MessageEntry,
    PromptEntry,
    SessionCursor,
    ToolCallEntry,
    TurnCursor,
)
from haku.console.conversation_read_access import ConversationAccessDeniedError, ProfileScopedReads, UnrestrictedReads
from haku.console.database_schema import (
    Conversation,
    ConversationEventRow,
    ConversationItem,
    ConversationPrompt,
    ConversationTurn,
    HttpGrantRow,
    KubernetesGrantRow,
    McpToolCall,
    McpToolCallPrincipal,
    Session,
)
from haku.console.grants.envelope import GrantStatus
from haku.console.grants.http.models import HttpMethod, HttpScheme
from haku.console.grants.kubernetes.models import KubernetesNamespacesGrantScope, KubernetesRule
from haku.console.grants.principal import GrantPrincipalKind
from haku.console.notifications.session_wakes import SessionEvent, SessionEventKind
from haku.console.session.conftest import age_lease, answers, attach_channel, lease_of, make_idle
from haku.console.session.setup_output import SETUP_OUTPUT_KIND
from haku.console.session.store import (
    ADOPTION_GRACE,
    REPLICA,
    BridgeAuthentication,
    PositionUnusableError,
    PromptRefusedError,
    Store,
    TurnState,
)
from haku.console.tool_calls import ToolCallStatus
from haku.console.x.claude_code.testing.wire import assistant, result, text_block, text_delta
from haku.console.x.conversation_events import (
    CallRef,
    ItemSegment,
    MessageCompleted,
    MessageStarted,
    OpenRef,
    ToolCallCompleted,
    ToolCallStarted,
)
from haku.console.x.runtime import RuntimeAdapter, RuntimeRegistry

ROOM = "!room:example.org"


def _harness(frames: Sequence[FrameRecord]) -> list[HarnessFrameRecord]:
    """Narrow a frame page to the harness variant, which these reads are asserting about."""
    assert all(isinstance(frame, HarnessFrameRecord) for frame in frames)
    return cast(list[HarnessFrameRecord], list(frames))


class _AlternateFrameVocabulary:
    """A harness whose native JSON has no conventional discriminator keys."""

    kind = RuntimeKind.CLAUDE_CODE

    def prompt_submitted(self, outbound) -> bool:
        return any(frame.frame.get("动作") == "提问" for frame in outbound)


async def test_store_delegates_prompt_semantics_and_keeps_native_json_opaque(migrated_sessions, operator_id) -> None:
    runtime = cast(RuntimeAdapter, _AlternateFrameVocabulary())
    store = Store(migrated_sessions, RuntimeRegistry({RuntimeKind.CLAUDE_CODE: runtime}))
    view, token = await store._create_provisioning_for_test(operator_id)
    assert await store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await store.enqueue_prompt(operator_id, view.session_id, "question", SPA_ORIGIN)
    assert await store.next_prompt(view.session_id) is not None
    await store.record_frame(
        view.session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"动作": "提问", "正文": "hello"}
    )
    await store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"阶段": "碎片", "正文": "你"}
    )
    await store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"阶段": "最终", "正文": "你好"}
    )

    assert await store.adopt_open_turn(view.session_id) is not None
    frames = await store.read_session_frames(view.session_id, cursor=None, limit=25, scope=UnrestrictedReads())
    assert [frame.payload for frame in _harness(frames)] == [
        {"动作": "提问", "正文": "hello"},
        {"阶段": "碎片", "正文": "你"},
        {"阶段": "最终", "正文": "你好"},
    ]


async def test_bridge_authentication_distinguishes_accept_terminal_and_rejected(
    session_store, operator_id, migrated_sessions
) -> None:
    view, token = await session_store.create(operator_id)
    session_id = view.session_id

    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    async with migrated_sessions() as db:
        record = await db.get(Session, session_id)
        assert record is not None
        assert record.status == SessionStatus.READY
        assert record.bridge_connected_at is not None
        # Only the hash is ever kept: it lets a retrying runner prove which session it belongs to
        # without the bearer being retained or recoverable.
        assert record.bridge_token_fingerprint == Store._fingerprint(token)

    await session_store.fail(session_id, "runner failed")
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.TERMINAL
    assert await session_store.authenticate_bridge(session_id, "wrong") == BridgeAuthentication.REJECTED


async def test_an_idle_session_has_no_bridge_credential_to_authenticate(
    session_store, operator_id, migrated_sessions
) -> None:
    view, _ = await session_store.create(operator_id)
    await make_idle(migrated_sessions, view.session_id)

    assert await session_store.authenticate_bridge(view.session_id, "anything") == BridgeAuthentication.REJECTED


async def test_the_first_idle_prompt_mints_exactly_one_allocation(
    session_store, operator_id, migrated_sessions
) -> None:
    view, _ = await session_store.create(operator_id)
    await make_idle(migrated_sessions, view.session_id)
    await session_store.enqueue_prompt(operator_id, view.session_id, "wake up", SPA_ORIGIN)

    first, second = await asyncio.gather(
        session_store.allocate(operator_id, view.session_id), session_store.allocate(operator_id, view.session_id)
    )

    allocation = one(candidate for candidate in (first, second) if candidate is not None)
    assert allocation.session_id == view.session_id
    assert await session_store.status(view.session_id) == SessionStatus.PROVISIONING
    async with migrated_sessions() as db:
        record = await db.get(Session, view.session_id)
        assert record is not None
        assert record.bridge_token_fingerprint == Store._fingerprint(allocation.bridge_token)
    assert (
        len(await authored_events_of_kind(migrated_sessions, view.session_id, AuthoredEventKind.SESSION_PROVISIONING))
        == 2
    ), "the test's eager predecessor event plus the allocation transition"


async def test_idle_without_work_does_not_allocate(session_store, operator_id, migrated_sessions) -> None:
    view, _ = await session_store.create(operator_id)
    await make_idle(migrated_sessions, view.session_id)

    assert await session_store.allocate(operator_id, view.session_id) is None
    assert await session_store.status(view.session_id) == SessionStatus.IDLE


async def test_an_idle_session_can_close_without_ever_minting_a_credential(
    session_store, operator_id, migrated_sessions
) -> None:
    view, _ = await session_store.create(operator_id)
    await make_idle(migrated_sessions, view.session_id)

    await session_store.request_close(operator_id, view.session_id)
    await session_store.complete_claim_cleanup(view.session_id)

    assert await session_store.status(view.session_id) == SessionStatus.CLOSED


async def test_deliberate_close_is_not_reclassified_as_runner_failure(
    session_store, operator_id, migrated_sessions
) -> None:
    view, token = await session_store.create(operator_id)

    await session_store.request_close(operator_id, view.session_id)
    await session_store.fail(view.session_id, "sandbox runner disconnected")
    closing = await session_store.get(operator_id, view.session_id)
    assert closing.status == SessionStatus.CLOSING
    assert closing.error is None

    await session_store.complete_claim_cleanup(view.session_id)
    closed = await session_store.get(operator_id, view.session_id)
    assert closed.status == SessionStatus.CLOSED
    async with migrated_sessions() as db:
        record = await db.get(Session, view.session_id)
        assert record is not None
        assert record.claim_cleaned_at is not None
        # The credential column is untouched by cleanup: it verifies, it does not also record
        # that the sandbox is gone.
        assert record.bridge_token_fingerprint == Store._fingerprint(token)
    ended = one(await authored_events_of_kind(migrated_sessions, view.session_id, AuthoredEventKind.SESSION_ENDED))
    assert ended.body == {"status": SessionStatus.CLOSED, "error": None}


async def test_failure_records_the_final_status_and_error_once(session_store, migrated_sessions, operator_id) -> None:
    view, _ = await session_store.create(operator_id)

    await session_store.fail(view.session_id, "runner failed")
    await session_store.fail(view.session_id, "a later observer also noticed")

    ended = one(await authored_events_of_kind(migrated_sessions, view.session_id, AuthoredEventKind.SESSION_ENDED))
    assert ended.body == {"status": SessionStatus.FAILED, "error": "runner failed"}
    failed = await session_store.get(operator_id, view.session_id)
    assert (failed.status, failed.error) == (SessionStatus.FAILED, "runner failed")


async def test_session_end_terminalizes_exact_session_grants(session_store, migrated_sessions, operator_id) -> None:
    agent_id = UUID("00000000-0000-4000-8000-000000000001")
    reservation_id, binding_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    async with migrated_sessions.begin() as db:
        await db.execute(
            text(
                """
                INSERT INTO agent_name_reservations (
                    reservation_id, display_name, display_name_key, agent_id, created_at, activated_at
                ) VALUES (:reservation_id, 'Session Grant Agent', 'session grant agent', :agent_id, :n, :n)
                """
            ),
            {"reservation_id": reservation_id, "agent_id": agent_id, "n": now},
        )
        await db.execute(
            text(
                """
                INSERT INTO agents (
                    agent_id, owner_operator_id, current_name_reservation_id, status,
                    created_at, updated_at, activated_at, access_profile_id
                ) VALUES (
                    :agent_id, :operator_id, :reservation_id, 'active', :n, :n, :n, 'no_auto_approval'
                )
                """
            ),
            {"agent_id": agent_id, "operator_id": operator_id, "reservation_id": reservation_id, "n": now},
        )
        await db.execute(
            text(
                """
                INSERT INTO credential_bindings (
                    binding_id, agent_id, kind, status, generation, created_at, updated_at,
                    issued_at, activated_at
                ) VALUES (:binding_id, :agent_id, 'static', 'active', 1, :n, :n, :n, :n)
                """
            ),
            {"binding_id": binding_id, "agent_id": agent_id, "n": now},
        )
        await db.execute(
            text(
                """
                INSERT INTO static_credentials (
                    binding_id, secret_reference, credential_fingerprint, created_at
                ) VALUES (:binding_id, :reference, :fingerprint, :n)
                """
            ),
            {
                "binding_id": binding_id,
                "reference": f"env:SESSION_GRANT_{agent_id}",
                "fingerprint": binding_id.bytes,
                "n": now,
            },
        )
    view, _ = await session_store.create(operator_id, agent_id=agent_id, access_profile_id="no_auto_approval")
    grant_id = uuid4()
    async with migrated_sessions.begin() as db:
        session = await db.get(Session, view.session_id)
        assert session is not None
        session.agent_binding_id = binding_id
        session.bridge_connected_at = datetime.now(UTC)
        session.lease_expires_at = datetime(2999, 1, 1, tzinfo=UTC)
        await db.flush([session])
        source_tool_call_id = f"tc_{uuid4().hex}"
        db.add(
            McpToolCall(
                tool_call_id=source_tool_call_id,
                server_id="kubernetes",
                tool_name="create_grant",
                status=ToolCallStatus.RUNNING,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                arguments_json={},
                rationale="session grant terminalization test",
                title=None,
                result_json=None,
                error=None,
                denial_reason=None,
                withdrawal_reason=None,
                approval_policy_id=None,
                auto_approval_evaluation=None,
                approved_at=datetime.now(UTC),
            )
        )
        db.add(
            McpToolCallPrincipal(
                tool_call_id=source_tool_call_id, operator_id=None, binding_id=binding_id, session_id=view.session_id
            )
        )
        await db.flush()
        db.add(
            KubernetesGrantRow(
                grant_id=grant_id,
                owner_agent_id=agent_id,
                principal_kind=GrantPrincipalKind.SESSION,
                principal_agent_id=None,
                principal_session_id=view.session_id,
                source_tool_call_id=source_tool_call_id,
                scope=KubernetesNamespacesGrantScope(namespaces=("public-coder-agent",)),
                rules=[KubernetesRule(api_groups=("",), resources=("pods/log",), verbs=("get",))],
                status=GrantStatus.ACTIVE,
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                ended_at=None,
                end_reason=None,
            )
        )

    http_grant_id = uuid4()
    async with migrated_sessions.begin() as db:
        http_source_tool_call_id = f"tc_{uuid4().hex}"
        db.add(
            McpToolCall(
                tool_call_id=http_source_tool_call_id,
                server_id="http_grants",
                tool_name="create_grant",
                status=ToolCallStatus.RUNNING,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                arguments_json={},
                rationale="session grant terminalization test",
                title=None,
                result_json=None,
                error=None,
                denial_reason=None,
                withdrawal_reason=None,
                approval_policy_id=None,
                auto_approval_evaluation=None,
                approved_at=datetime.now(UTC),
            )
        )
        db.add(
            McpToolCallPrincipal(
                tool_call_id=http_source_tool_call_id,
                operator_id=None,
                binding_id=binding_id,
                session_id=view.session_id,
            )
        )
        await db.flush()
        db.add(
            HttpGrantRow(
                grant_id=http_grant_id,
                owner_agent_id=agent_id,
                principal_kind=GrantPrincipalKind.SESSION,
                principal_agent_id=None,
                principal_session_id=view.session_id,
                source_tool_call_id=http_source_tool_call_id,
                scheme=HttpScheme.HTTPS,
                host="grocy.example",
                port=443,
                methods=frozenset({HttpMethod.GET}),
                path_regex=None,
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                released_at=None,
                revoked_at=None,
                end_reason=None,
            )
        )

    await session_store.fail(view.session_id, "runner failed")

    async with migrated_sessions() as db:
        grant = await db.get(KubernetesGrantRow, grant_id)
        assert grant is not None
        assert grant.status is GrantStatus.REVOKED
        assert grant.end_reason == "principal_ended"
        assert grant.ended_at is not None
        http_grant = await db.get(HttpGrantRow, http_grant_id)
        assert http_grant is not None
        assert http_grant.revoked_at is not None
        assert http_grant.released_at is None
        assert http_grant.end_reason == "principal_ended"


async def test_the_cleanup_sweep_offers_ended_sessions_until_their_claim_is_recorded_gone(
    session_store, operator_id
) -> None:
    """Two facts, two columns: liveness gates the candidate set, `claim_cleaned_at` empties it, so
    an interrupted teardown is retryable and a completed one final."""
    live, _ = await session_store.create(operator_id)
    swept, _ = await session_store.create(operator_id)
    cleaned, _ = await session_store.create(operator_id)
    for session in (swept, cleaned):
        await session_store.fail(session.session_id, "runner failed")

    assert sorted(await session_store.claim_cleanup_candidates()) == sorted([swept.session_id, cleaned.session_id])

    await session_store.complete_claim_cleanup(cleaned.session_id)
    assert await session_store.claim_cleanup_candidates() == [swept.session_id]
    assert live.session_id not in await session_store.claim_cleanup_candidates()


async def test_a_cleaned_up_session_admits_nobody_and_says_which_of_the_two_reasons(session_store, operator_id) -> None:
    """The credential survives cleanup, so refusal is the status's doing — which is what tells a
    runner holding the right token to stop (`TERMINAL`) apart from one holding the wrong one
    (`REJECTED`)."""
    view, token = await session_store.create(operator_id)
    await session_store.request_close(operator_id, view.session_id)
    await session_store.complete_claim_cleanup(view.session_id)

    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.TERMINAL
    assert await session_store.authenticate_bridge(view.session_id, "wrong") == BridgeAuthentication.REJECTED


async def test_how_far_a_turn_has_got_is_derived_from_the_items_it_opened(
    session_store, migrated_sessions, operator_id
) -> None:
    """Which is what replaces the two columns the turn used to carry.

    What a turn is streaming into is its one open message item, and whether it said anything is
    whether it has a completed one — so there is one place either fact can be wrong rather than two
    that can disagree, and a replica adopting the turn reads the same answer.
    """
    view, token = await session_store.create(operator_id)
    session_id = view.session_id
    await attach_channel(migrated_sessions, session_id, ROOM)
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, session_id, "why did it fail?", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    assert await session_store.turn_state(started.turn_id) == TurnState(streaming=None, said_anything=False)

    where = FrameRange(1, 1)
    await session_store.apply_frame(session_id, started.turn_id, 1, [MessageStarted(provenance=where)])
    await session_store.apply_frame(
        session_id,
        started.turn_id,
        2,
        [ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text="a bad config", provenance=FrameRange(2, 2))],
    )
    assert await session_store.turn_state(started.turn_id) == TurnState(streaming="a bad config", said_anything=False)

    await session_store.apply_frame(
        session_id, started.turn_id, 3, [MessageCompleted(backend_item_id=None, provenance=FrameRange(3, 3))]
    )

    assert await session_store.turn_state(started.turn_id) == TurnState(streaming=None, said_anything=True)
    assert await answers(migrated_sessions, session_id) == ["a bad config"]


async def test_the_rollout_reads_back_in_wire_order_with_a_keyset_cursor(session_store, operator_id) -> None:
    """Keyset, not offset: the log is append-only, so new frames landing between pages would
    make an offset skip or repeat a row."""
    session, _ = await session_store.create(operator_id)
    for kind in ("user", "assistant", "result"):
        await session_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": kind}
        )

    first = await session_store.read_session_frames(
        str(session.session_id), cursor=None, limit=2, scope=UnrestrictedReads()
    )
    rest = await session_store.read_session_frames(
        str(session.session_id), cursor=first[-1].frame_seq + 1, limit=2, scope=UnrestrictedReads()
    )

    assert [frame.kind for frame in first] == ["harness_frame", "harness_frame"]
    assert [frame.payload["type"] for frame in _harness(first)] == ["user", "assistant"]
    assert [frame.payload["type"] for frame in _harness(rest)] == ["result"]


async def test_the_kinds_filter_uses_only_hakus_outer_bridge_class(session_store, operator_id) -> None:
    session, _ = await session_store.create(operator_id)
    await session_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, {"text": "booting"}
    )
    await session_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"阶段": "最终"}
    )

    default = await session_store.read_session_frames(
        str(session.session_id), cursor=None, limit=25, scope=UnrestrictedReads()
    )
    setup = await session_store.read_session_frames(
        str(session.session_id), cursor=None, limit=25, kinds=[BridgeFrameKind.SETUP_OUTPUT], scope=UnrestrictedReads()
    )
    harness = await session_store.read_session_frames(
        str(session.session_id), cursor=None, limit=25, kinds=[BridgeFrameKind.HARNESS_FRAME], scope=UnrestrictedReads()
    )

    assert [(frame.kind, frame.payload) for frame in _harness(default)] == [("harness_frame", {"阶段": "最终"})]
    assert [(frame.kind, frame.text) for frame in setup] == [("setup_output", "booting")]
    assert [(frame.kind, frame.payload) for frame in _harness(harness)] == [("harness_frame", {"阶段": "最终"})]


async def test_method_only_native_frames_are_visible_and_filterable(session_store, operator_id) -> None:
    session, _ = await session_store.create(operator_id)
    payload = {"jsonrpc": "2.0", "method": "codex/event/unknown", "params": {"opaque": True}}
    inner = payload
    recorded = await session_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, inner
    )

    default = await session_store.read_session_frames(
        str(session.session_id), cursor=None, limit=25, scope=UnrestrictedReads()
    )
    exact = await session_store.read_session_frames(
        session.session_id, cursor=recorded.frame_seq, limit=1, scope=UnrestrictedReads()
    )

    assert [frame.payload for frame in _harness(default)] == [inner]
    assert [frame.payload for frame in _harness(exact)] == [inner]


async def test_native_frames_without_a_known_discriminator_remain_in_the_default_and_exact_views(
    session_store, operator_id
) -> None:
    session, _ = await session_store.create(operator_id)
    payload = {"jsonrpc": "2.0", "id": 7, "result": {"opaque": True}}
    inner = payload
    recorded = await session_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, inner
    )

    default = await session_store.read_session_frames(
        str(session.session_id), cursor=None, limit=25, scope=UnrestrictedReads()
    )
    exact = await session_store.read_session_frames(
        session.session_id, cursor=recorded.frame_seq, limit=1, scope=UnrestrictedReads()
    )

    assert [frame.payload for frame in _harness(default)] == [inner]
    assert [frame.payload for frame in _harness(exact)] == [inner]


async def test_a_replayed_frame_is_recorded_once(session_store, operator_id) -> None:
    """An adopted connection re-sends whatever the previous console may not have acknowledged, and
    the runner's sequence position is what recognises it. The cursor is an optimisation; this is
    what makes replay safe."""
    session, _ = await session_store.create(operator_id)
    frame = assistant(message_id="msg_01abc")

    assert (
        await session_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, frame, runner_seq=1
        )
    ).fresh
    assert not (
        await session_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, frame, runner_seq=1
        )
    ).fresh

    frames = await session_store.read_session_frames(
        str(session.session_id), cursor=None, limit=25, scope=UnrestrictedReads()
    )
    assert [frame.kind for frame in frames] == ["harness_frame"]
    assert [frame.payload["type"] for frame in _harness(frames)] == ["assistant"]


async def test_the_resume_cursor_is_the_highest_number_a_runner_gave_this_session(session_store, operator_id) -> None:
    """What a reconnecting console hands back, per session rather than per connection: two consoles
    can be adopting one runner's window during a roll, so the cursor has to be a fact about the log
    both can read. It ignores rows no runner numbered, and need not be the newest row — a
    `setup_output` recorded after them carries no number of its own.
    """
    session, _ = await session_store.create(operator_id)
    other, _ = await session_store.create(operator_id)
    assert await session_store.highest_runner_seq(session.session_id) is None

    await session_store.record_frame(
        session.session_id,
        FrameDirection.FROM_AGENT,
        BridgeFrameKind.HARNESS_FRAME,
        {"type": "assistant"},
        runner_seq=4,
    )
    await session_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "result"}, runner_seq=9
    )
    await session_store.record_frame(
        session.session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    # A neighbouring session's numbering is its own runner's and says nothing about this one.
    await session_store.record_frame(
        other.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "result"}, runner_seq=99
    )

    assert await session_store.highest_runner_seq(session.session_id) == 9


async def test_two_sessions_may_hold_the_same_agent_id(session_store, operator_id) -> None:
    """The index is per session, because a replacement session re-awakened from the room can be
    handed the same message ids by an agent with no idea it is a second session."""
    mine, _ = await session_store.create(operator_id)
    theirs, _ = await session_store.create(operator_id)
    frame = assistant(message_id="msg_01abc")

    assert (
        await session_store.record_frame(
            mine.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, frame
        )
    ).fresh
    assert (
        await session_store.record_frame(
            theirs.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, frame
        )
    ).fresh


async def test_frames_with_no_identity_are_never_collapsed(session_store, operator_id) -> None:
    """ "No identity" is not "the same as the last one". Deltas and console-authored rows have
    none, and two of them are two frames."""
    session, _ = await session_store.create(operator_id)
    delta = {"type": "stream_event", "event": {"type": "content_block_delta"}}

    assert (
        await session_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, delta
        )
    ).fresh
    assert (
        await session_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, delta
        )
    ).fresh

    frames = await session_store.read_session_frames(
        str(session.session_id), cursor=None, limit=25, scope=UnrestrictedReads()
    )
    assert len(frames) == 2


async def test_the_raw_log_returns_every_native_frame_without_classifying_it(session_store, operator_id) -> None:
    session, _ = await session_store.create(operator_id)
    session_id = session.session_id
    await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "stream_event", "event": {}}
    )
    await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "result", "uuid": "r1"}
    )

    default = await session_store.read_session_frames(str(session_id), cursor=None, limit=25, scope=UnrestrictedReads())

    assert [frame.kind for frame in default] == ["harness_frame", "harness_frame"]
    assert [frame.payload["type"] for frame in _harness(default)] == ["stream_event", "result"]


async def test_one_session_never_reads_another_session_frames(session_store, operator_id) -> None:
    mine, _ = await session_store.create(operator_id)
    theirs, _ = await session_store.create(operator_id)
    await session_store.record_frame(
        mine.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "assistant"}
    )
    await session_store.record_frame(
        theirs.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "result"}
    )

    frames = await session_store.read_session_frames(
        str(mine.session_id), cursor=None, limit=25, scope=UnrestrictedReads()
    )

    assert [frame.kind for frame in frames] == ["harness_frame"]
    assert [frame.payload["type"] for frame in _harness(frames)] == ["assistant"]


async def test_the_frame_inspector_opens_on_the_end_of_the_log_and_walks_back(session_store, operator_id) -> None:
    """The console's read is the reverse keyset of the MCP reader's: a long session's interesting
    frames are its last ones, so the first page is the tail and the cursor walks towards the start.
    Each page itself stays in wire order.
    """
    session, _ = await session_store.create(operator_id)
    for kind in ("system", "user", "assistant", "result"):
        await session_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": kind}
        )

    newest = await session_store.read_operator_frames(
        operator_id, session.session_id, before_seq=None, limit=2, kinds=None
    )
    earlier = await session_store.read_operator_frames(
        operator_id, session.session_id, before_seq=newest.next_before_seq, limit=2, kinds=None
    )

    assert [frame.kind for frame in newest.frames] == ["harness_frame", "harness_frame"]
    assert [frame.payload["type"] for frame in newest.frames] == ["assistant", "result"]
    assert [frame.kind for frame in earlier.frames] == ["harness_frame", "harness_frame"]
    assert [frame.payload["type"] for frame in earlier.frames] == ["system", "user"]
    # A short page is the first one; this page is full, so whether it is the first is unknown.
    assert earlier.next_before_seq == earlier.frames[0].frame_seq


async def test_the_frame_inspector_dumps_native_frames_without_classifying_them(session_store, operator_id) -> None:
    session, _ = await session_store.create(operator_id)
    session_id = session.session_id
    await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "stream_event", "event": {}}
    )
    await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "result"}
    )

    default = await session_store.read_operator_frames(operator_id, session_id, before_seq=None, limit=25)

    assert [frame.kind for frame in default.frames] == ["harness_frame", "harness_frame"]
    assert [frame.payload["type"] for frame in default.frames] == ["stream_event", "result"]
    assert default.next_before_seq is None


async def test_the_frame_inspector_refuses_a_session_another_operator_owns(session_store, operator_id) -> None:
    """The MCP reader is deliberately unscoped; a browser surface must never be."""
    session, _ = await session_store.create(operator_id)
    await session_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "result"}
    )

    with pytest.raises(KeyError):
        await session_store.read_operator_frames(uuid4(), session.session_id, before_seq=None, limit=25, kinds=None)


async def test_a_frame_reaches_the_inspector_with_its_payload_whole(session_store, operator_id) -> None:
    """No clipping on this path: the MCP reader clips for context budget, but here the wire *is* the
    answer."""
    session, _ = await session_store.create(operator_id)
    payload = {
        "type": "user",
        "kind": "native-collision",
        "seq": 99,
        "frame": "native-collision",
        "message": {"content": [{"type": "tool_result", "content": "x" * 20_000}]},
    }
    await session_store.record_frame(
        session.session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, payload
    )

    page = await session_store.read_operator_frames(
        operator_id, session.session_id, before_seq=None, limit=25, kinds=None
    )

    assert page.frames[0].payload == payload
    assert page.frames[0].direction == FrameDirection.TO_AGENT
    assert page.harness_kind == "claude_code"


async def test_sessions_come_back_newest_first_with_the_channels_holding_their_thread(
    session_store, migrated_sessions, operator_id
) -> None:
    """The attachments, not a surface enum: a session says which channels hold a copy of the
    conversation it runs, which is the shape that survives a second one attaching."""
    await session_store.create(operator_id)
    matrix, _ = await session_store.create(operator_id)
    await attach_channel(migrated_sessions, matrix.session_id, "!room:example.org")

    sessions = await session_store.list_sessions(cursor=None, limit=10, scope=UnrestrictedReads())

    assert sessions[0].session_id == matrix.session_id
    assert sessions[0].harness_kind == "claude_code"
    assert [attachment.address for attachment in sessions[0].attachments] == ["!room:example.org"]
    assert sessions[1].attachments == []


async def test_a_session_created_between_two_pages_cannot_shift_what_the_second_one_holds(
    session_store, operator_id
) -> None:
    """This order grows at the top and an offset counts from there, so a session created mid-walk
    would push the first page's last row into the second page again."""
    older, _ = await session_store.create(operator_id)
    newer, _ = await session_store.create(operator_id)

    # Two rows for a page of one: the extra row is the one the tool's cursor names.
    first, resume = await session_store.list_sessions(cursor=None, limit=2, scope=UnrestrictedReads())
    await session_store.create(operator_id)
    second = await session_store.list_sessions(cursor=SessionCursor.of(resume), limit=1, scope=UnrestrictedReads())

    assert first.session_id == newer.session_id
    assert [session.session_id for session in second] == [older.session_id]


async def test_two_sessions_created_in_one_instant_are_paged_exactly_once_each(
    session_store, operator_id, migrated_sessions
) -> None:
    """`created_at` ties, so it does not order the corpus on its own — a cursor naming only the
    timestamp would either step over one of the pair or hand it out on both pages."""
    first, _ = await session_store.create(operator_id)
    second, _ = await session_store.create(operator_id)
    async with migrated_sessions.begin() as db:
        await db.execute(
            update(Session)
            .where(Session.session_id.in_([first.session_id, second.session_id]))
            .values(created_at=datetime(2026, 8, 12, 9, 0, tzinfo=UTC))
        )

    page, resume = await session_store.list_sessions(cursor=None, limit=2, scope=UnrestrictedReads())
    rest = await session_store.list_sessions(cursor=SessionCursor.of(resume), limit=10, scope=UnrestrictedReads())

    assert [session.session_id for session in [page, *rest]] == sorted(
        [first.session_id, second.session_id], reverse=True
    )


async def _pinned_agent(sessions, operator_id: UUID) -> UUID:
    """A minimal durable Agent row a conversation's pinned identity can reference."""
    agent_id, reservation_id = uuid4(), uuid4()
    async with sessions.begin() as db:
        # The reservation's agent FK is DEFERRED, so the pair lands in one transaction.
        await db.execute(
            text(
                "INSERT INTO agent_name_reservations "
                "(reservation_id, display_name, display_name_key, agent_id, created_at, activated_at) "
                "VALUES (:reservation_id, :name, :name, :agent_id, now(), now())"
            ),
            {"reservation_id": reservation_id, "name": str(agent_id), "agent_id": agent_id},
        )
        await db.execute(
            text(
                "INSERT INTO agents "
                "(agent_id, owner_operator_id, current_name_reservation_id, status, "
                " created_at, updated_at, activated_at, access_profile_id) "
                "VALUES (:agent_id, :operator_id, :reservation_id, 'active', now(), now(), now(), 'haku')"
            ),
            {"agent_id": agent_id, "operator_id": operator_id, "reservation_id": reservation_id},
        )
    return agent_id


async def test_reads_are_fenced_by_the_conversations_pinned_profile(
    session_store, migrated_sessions, operator_id
) -> None:
    """The profile-DAG scope is the row fence for every `haku_conversations` read.

    The listing filters; a point read of a session or conversation outside the scope refuses
    loudly; a conversation predating pinned identity is readable only by the unrestricted
    (Operator) scope; and an unknown id stays an empty page rather than a denial.
    """
    agent_id = await _pinned_agent(migrated_sessions, operator_id)
    pinned, _ = await session_store.create(operator_id, agent_id=agent_id, access_profile_id="haku")
    legacy, _ = await session_store.create(operator_id)
    haku_reads = ProfileScopedReads(readable_profile_ids=frozenset({"haku"}))
    coder_reads = ProfileScopedReads(readable_profile_ids=frozenset({"public-coder"}))

    listed = await session_store.list_sessions(cursor=None, limit=10, scope=haku_reads)
    assert [session.session_id for session in listed] == [pinned.session_id]
    everything = await session_store.list_sessions(cursor=None, limit=10, scope=UnrestrictedReads())
    assert {session.session_id for session in everything} == {pinned.session_id, legacy.session_id}
    assert await session_store.list_sessions(cursor=None, limit=10, scope=ProfileScopedReads(frozenset())) == []

    assert await session_store.read_session_frames(pinned.session_id, cursor=None, limit=5, scope=haku_reads) == []
    for denied_scope, session_id in ((coder_reads, pinned.session_id), (haku_reads, legacy.session_id)):
        with pytest.raises(ConversationAccessDeniedError):
            await session_store.read_session_frames(session_id, cursor=None, limit=5, scope=denied_scope)
        with pytest.raises(ConversationAccessDeniedError):
            await session_store.list_turns(session_id, cursor=None, limit=5, scope=denied_scope)
    async with migrated_sessions() as db:
        legacy_conversation = await db.scalar(
            select(Session.conversation_id).where(Session.session_id == legacy.session_id)
        )
    with pytest.raises(ConversationAccessDeniedError):
        await session_store.read_item_rows(legacy_conversation, after_seq=None, limit=5, scope=haku_reads)

    assert await session_store.read_session_frames(uuid4(), cursor=None, limit=5, scope=coder_reads) == []
    assert await session_store.read_item_rows(uuid4(), after_seq=None, limit=5, scope=coder_reads) == []


async def test_a_prompt_records_the_channel_events_it_was_folded_from(
    session_store, operator_id, migrated_sessions
) -> None:
    """The store carries a surface's origin without reading it: the arm says whose it is, and
    nothing here knows what a Matrix room or event id is.

    The SPA is named rather than left absent, so the reader this exists for cannot confuse "typed
    into a browser" with "we never wrote it down" — which are opposite answers to "does this room
    already have a copy?".
    """
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(
        operator_id, view.session_id, "first\nsecond", MatrixOrigin(address=ROOM, refs=("$a", "$b"))
    )
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    await session_store.end_turn(turn.turn_id, TurnAnswered())
    await session_store.enqueue_prompt(operator_id, view.session_id, "do the thing", SPA_ORIGIN)

    asked = [
        PromptOpened.model_validate(row.body)
        for row in await item_events(migrated_sessions, view.session_id)
        if row.kind == ConversationEventKind.ITEM_OPENED and row.body.get("item_type") == ItemType.PROMPT
    ]

    assert [body.origin for body in asked] == [MatrixOrigin(address=ROOM, refs=("$a", "$b")), SpaOrigin()]


async def test_exchanges_page_by_their_own_keyset(session_store, operator_id) -> None:
    """`(started_at, turn_id)`, because two exchanges of one session can share a start instant and
    a cursor naming only the timestamp would step over one of a tied pair."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    for index in range(3):
        await session_store.enqueue_prompt(operator_id, view.session_id, f"prompt {index}", SPA_ORIGIN)
        turn = await session_store.next_prompt(view.session_id)
        assert turn is not None
        await session_store.end_turn(turn.turn_id, TurnAnswered())

    # One row past the page, exactly as the tool asks: the cursor names the first row not returned.
    *page, resume = await session_store.list_turns(view.session_id, cursor=None, limit=3, scope=UnrestrictedReads())
    rest = await session_store.list_turns(
        view.session_id, cursor=TurnCursor.of(resume), limit=5, scope=UnrestrictedReads()
    )

    assert len(page) == 2
    assert [turn.turn_id for turn in rest] == [resume.turn_id]


async def test_a_turn_ends_at_the_frame_it_names_rather_than_at_the_head_of_the_log(session_store, operator_id) -> None:
    """The CLI emits a `command_lifecycle` frame just after the `result` one and the recorder writes
    it while the turn is still being closed, so a bound taken from the log swallows a frame the turn
    did not produce."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    ending = await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, result(uuid="r1")
    )
    await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "command_lifecycle"}
    )

    await session_store.end_turn(turn.turn_id, TurnAnswered(), last_frame_seq=ending.frame_seq)

    [record] = await session_store.list_turns(view.session_id, cursor=None, limit=5, scope=UnrestrictedReads())
    assert record.last_frame_seq == ending.frame_seq


async def test_a_turn_that_ended_on_no_frame_is_bounded_by_the_ones_it_recorded(session_store, operator_id) -> None:
    """A failure has no ending frame to name, and the session's log is not a bound either: what
    came before the turn opened belongs to no turn of its own, and reporting it would hand a reader
    a range that ends before it starts."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "system"}
    )
    await session_store.enqueue_prompt(operator_id, view.session_id, "first", SPA_ORIGIN)
    silent = await session_store.next_prompt(view.session_id)
    assert silent is not None
    await session_store.end_turn(silent.turn_id, TurnFailed(failure="the runtime went away"))
    await session_store.enqueue_prompt(operator_id, view.session_id, "second", SPA_ORIGIN)
    spoke = await session_store.next_prompt(view.session_id)
    assert spoke is not None
    answer = await session_store.record_frame(
        view.session_id,
        FrameDirection.FROM_AGENT,
        BridgeFrameKind.HARNESS_FRAME,
        assistant(text_block("half an answer")),
    )

    await session_store.end_turn(spoke.turn_id, TurnFailed(failure="the runtime went away"))

    brackets = {
        record.turn_id: (record.first_frame_seq, record.last_frame_seq)
        for record in await session_store.list_turns(view.session_id, cursor=None, limit=5, scope=UnrestrictedReads())
    }
    assert brackets[silent.turn_id][1] is None, "it recorded nothing, and the frame before it is not its own"
    assert brackets[spoke.turn_id] == (answer.frame_seq, answer.frame_seq)


async def _conversation_entries(session_store, conversation_id, *, after_seq=None, limit=100):
    """The store's page rows folded to entries, as `conversation_reader.ConversationReads` serves them."""
    return [
        entry_of(row)
        for row in await session_store.read_item_rows(
            conversation_id, after_seq=after_seq, limit=limit, scope=UnrestrictedReads()
        )
    ]


async def test_the_items_read_as_the_conversation_rather_than_the_protocol(session_store, operator_id) -> None:
    """What a conversation meant, with a way back to the frames it was read off."""
    view, token = await session_store.create(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    started = await session_store.next_prompt(view.session_id)
    assert started is not None
    spoke = await session_store.record_frame(
        view.session_id,
        FrameDirection.FROM_AGENT,
        BridgeFrameKind.HARNESS_FRAME,
        assistant(text_block("a bad config"), message_id="msg_1"),
    )
    where = FrameRange(spoke.frame_seq, spoke.frame_seq)
    await session_store.apply_frame(
        view.session_id,
        started.turn_id,
        spoke.frame_seq,
        [
            MessageStarted(provenance=where),
            ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text="a bad config", provenance=where),
            MessageCompleted(backend_item_id="msg_1", provenance=where),
        ],
    )
    await session_store.end_turn(started.turn_id, TurnAnswered(), last_frame_seq=spoke.frame_seq)

    entries = await _conversation_entries(session_store, conversation_id, limit=10)

    assert [entry.kind for entry in entries] == ["prompt", "message"]
    said = one(entry for entry in entries if isinstance(entry, MessageEntry))
    assert said.text == "a bad config"
    assert isinstance(said.provenance, FromFrames)
    assert said.provenance.session_id == view.session_id, "frames are session-level, so the appeal names whose"
    named = await session_store.read_session_frames(
        said.provenance.session_id,
        cursor=said.provenance.first_frame_seq,
        limit=1,
        kinds=None,
        scope=UnrestrictedReads(),
    )
    assert _harness(named)[0].payload["message"]["id"] == "msg_1", (
        "provenance points at the complete inner frame it was read off"
    )


async def test_the_items_read_hands_back_the_rows_the_writer_materialised(
    session_store, migrated_sessions, operator_id
) -> None:
    """`conversation_item.text` is the writer's own fold of the log's segments, so a read of the
    rows cannot disagree with the log — which a read re-derived from the frames could not promise,
    because a change to the projection would move one of them and not the other."""
    view, token = await session_store.create(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await _exchange(session_store, operator_id, view.session_id, "first?", "one")
    await _exchange(session_store, operator_id, view.session_id, "second?", "two")

    entries = await _conversation_entries(session_store, conversation_id)

    spoken = [entry.text for entry in entries if isinstance(entry, MessageEntry)]
    assert spoken == await answers(migrated_sessions, view.session_id)


async def test_an_item_page_resumes_at_its_cursor_without_refolding_the_thread(session_store, operator_id) -> None:
    """The cursor is a durable stream position, so pages concatenate to the whole read and a page
    is served from its position alone — page N of a long conversation costs what page one does."""
    view, token = await session_store.create(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    for index in range(3):
        await _exchange(session_store, operator_id, view.session_id, f"ask {index}", f"answer {index}")

    whole = await _conversation_entries(session_store, conversation_id)
    first = await _conversation_entries(session_store, conversation_id, limit=3)
    rest = await _conversation_entries(session_store, conversation_id, after_seq=whole[3].opened_seq)

    assert first + rest == whole
    assert [entry.opened_seq for entry in whole] == sorted({entry.opened_seq for entry in whole}), (
        "opening positions are unique"
    )


async def test_a_frame_the_fold_never_committed_is_not_an_item(session_store, operator_id) -> None:
    """The entries are the conversation's record, so what is on them is what the fold committed —
    never whatever the frame table happens to hold. `read_session_frames` still serves the frame by name."""
    view, token = await session_store.create(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, text_delta("h")
    )

    entries = await _conversation_entries(session_store, conversation_id, limit=10)
    frames = await session_store.read_session_frames(
        view.session_id, cursor=None, limit=10, kinds=None, scope=UnrestrictedReads()
    )

    assert entries == []
    assert len(frames) == 1, "the frame is recorded and readable; it just never became a fact"


async def test_a_call_and_its_answer_are_one_entry(session_store, operator_id) -> None:
    """The ask and the answer are one row and so one entry — the answer fields fill in when it
    arrives, and `status` is what says whether it has."""
    view, token = await session_store.create(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "look it up", SPA_ORIGIN)
    started = await session_store.next_prompt(view.session_id)
    assert started is not None
    asked = await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "assistant"}
    )
    await session_store.apply_frame(
        view.session_id,
        started.turn_id,
        asked.frame_seq,
        [
            ToolCallStarted(
                call_id="toolu_1",
                tool_name="Bash",
                arguments={"command": "ls"},
                provenance=FrameRange(asked.frame_seq, asked.frame_seq),
            )
        ],
    )
    answered = await session_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    await session_store.apply_frame(
        view.session_id,
        started.turn_id,
        answered.frame_seq,
        [
            ItemSegment(
                item=CallRef(call_id="toolu_1"),
                text="a.txt",
                provenance=FrameRange(answered.frame_seq, answered.frame_seq),
            ),
            ToolCallCompleted(
                item=CallRef(call_id="toolu_1"),
                outcome=ToolOutcome.SUCCEEDED,
                structured={"exit_code": 0},
                provenance=FrameRange(answered.frame_seq, answered.frame_seq),
            ),
        ],
    )

    entries = await _conversation_entries(session_store, conversation_id, limit=10)

    assert [entry.kind for entry in entries] == ["prompt", "tool_call"]
    call = one(entry for entry in entries if isinstance(entry, ToolCallEntry))
    assert (call.tool_name, call.arguments) == ("Bash", {"command": "ls"})
    assert (call.status, call.content, call.structured, call.outcome) == (
        ItemStatus.COMPLETE,
        "a.txt",
        {"exit_code": 0},
        "succeeded",
    )


async def test_the_items_read_spans_replaced_sessions(session_store, migrated_sessions, operator_id) -> None:
    """A conversation outlives its runners, so the read that follows one thread does not stop
    where a sandbox died; which session produced an entry is on its provenance."""
    view, token = await session_store.create(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await _exchange(session_store, operator_id, view.session_id, "first?", "one")
    await session_store.fail(view.session_id, "sandbox died")
    replacement, replacement_token = await session_store.create(operator_id, conversation_id=conversation_id)
    assert replacement.session_id != view.session_id
    assert await session_store.authenticate_bridge(replacement.session_id, replacement_token) == (
        BridgeAuthentication.ACCEPTED
    )
    await _exchange(session_store, operator_id, replacement.session_id, "second?", "two")

    entries = await _conversation_entries(session_store, conversation_id)

    spoken = [entry for entry in entries if isinstance(entry, MessageEntry)]
    assert [entry.text for entry in spoken] == ["one", "two"]
    assert [entry.provenance.session_id for entry in spoken if isinstance(entry.provenance, FromFrames)] == [
        view.session_id,
        replacement.session_id,
    ]


async def test_a_prompt_admitted_before_any_session_is_on_the_conversations_items(
    session_store, migrated_sessions, operator_id
) -> None:
    """A prompt buys the sandbox, so it is accepted before a runner exists and the rows recording
    it name no session at all. The read is keyed by the conversation, so the prompt is on it from
    admission rather than from whenever a session claims it."""
    view, _ = await session_store.create_idle(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    async with migrated_sessions.begin() as db:
        await db.delete(await db.get(Session, view.session_id))
    await session_store.enqueue_conversation_prompt(operator_id, conversation_id, "start", SPA_ORIGIN)

    entry = one(await _conversation_entries(session_store, conversation_id, limit=10))
    assert isinstance(entry, PromptEntry)
    assert (entry.text, entry.origin) == ("start", PromptOriginKind.SPA)


async def test_operator_conversation_read_surface_keeps_inventory_and_transcript_separate(
    session_store, migrated_sessions, operator_id
) -> None:
    """The Console list is light, while detail carries messages and turn summaries. Both are keyed
    by the conversation and carry its attachments, so a list row says which channels hold this
    thread rather than which surface a session was created for.
    """
    await session_store.create(operator_id)
    matrix, matrix_token = await session_store.create(operator_id)
    await attach_channel(migrated_sessions, matrix.session_id, ROOM)
    assert await session_store.authenticate_bridge(matrix.session_id, matrix_token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(
        operator_id, matrix.session_id, "What is happening?", MatrixOrigin(address=ROOM, refs=("$asked",))
    )
    conversation_id = await session_store.conversation_of(matrix.session_id)

    page = await session_store.list_operator_conversations(operator_id, cursor=None, limit=10)
    detail = await session_store.get_operator_conversation(operator_id, conversation_id)

    assert page.conversations[0].conversation_id == conversation_id
    assert page.conversations[0].harness_kind == "claude_code"
    assert [attachment.address for attachment in page.conversations[0].attachments] == [ROOM]
    assert page.conversations[0].live_session is not None
    assert page.conversations[0].live_session.session_id == matrix.session_id
    assert page.conversations[0].item_count == 1
    assert [attachment.address for attachment in detail.attachments] == [ROOM]
    assert detail.harness_kind == "claude_code"
    assert detail.session.session_id == matrix.session_id
    asked = one(entry for entry in detail.entries if isinstance(entry, PromptEntry))
    assert asked.text == "What is happening?"
    assert detail.earlier_sessions == []


async def test_a_conversation_a_channel_holds_takes_a_prompt_typed_in_the_browser(
    session_store, migrated_sessions, operator_id
) -> None:
    """One conversation, two surfaces. Nothing on the browser path asks what channel holds the
    thread, and nothing may start to: a room's session admits a prompt on exactly the terms an SPA
    session does.
    """
    matrix, token = await session_store.create(operator_id)
    await attach_channel(migrated_sessions, matrix.session_id, ROOM)
    assert await session_store.authenticate_bridge(matrix.session_id, token) == BridgeAuthentication.ACCEPTED

    await session_store.enqueue_prompt(operator_id, matrix.session_id, "typed into the tab", SPA_ORIGIN)
    detail = await session_store.get_operator_conversation(
        operator_id, await session_store.conversation_of(matrix.session_id)
    )

    typed = one(entry for entry in detail.entries if isinstance(entry, PromptEntry))
    assert typed.text == "typed into the tab"
    assert [attachment.address for attachment in detail.attachments] == [ROOM]


async def test_a_replacement_session_leaves_the_thread_and_its_attachment_where_they_were(
    session_store, migrated_sessions, operator_id
) -> None:
    """The successor runs the same thread, so the attachment is untouched and the transcript of the
    session that died stays reachable beside it."""
    first, _ = await session_store.create(operator_id)
    await attach_channel(migrated_sessions, first.session_id, ROOM)
    conversation_id = await session_store.conversation_of(first.session_id)
    await session_store.fail(first.session_id, "the sandbox went away")
    second, _ = await session_store.create(operator_id, conversation_id=conversation_id)

    page = await session_store.list_operator_conversations(operator_id, cursor=None, limit=10)
    detail = await session_store.get_operator_conversation(operator_id, conversation_id)

    assert [conversation.conversation_id for conversation in page.conversations] == [conversation_id]
    assert page.conversations[0].harness_kind == "claude_code"
    assert detail.harness_kind == "claude_code"
    assert detail.session.session_id == second.session_id
    assert [session.session_id for session in detail.earlier_sessions] == [first.session_id]
    assert [attachment.address for attachment in detail.attachments] == [ROOM]


async def test_a_conversation_whose_last_session_failed_says_so_in_the_inventory(session_store, operator_id) -> None:
    """A failed session is not live, so `live_session: null` alone would make a thread whose runner
    died read like any idle thread. The inventory says how the newest session ended — and only for
    conversations no session is holding, so a failed session already replaced reports nothing."""
    failed, _ = await session_store.create(operator_id)
    await session_store.fail(failed.session_id, "the sandbox went away")
    replaced, _ = await session_store.create(operator_id)
    await session_store.fail(replaced.session_id, "the sandbox went away")
    await session_store.create(operator_id, conversation_id=await session_store.conversation_of(replaced.session_id))
    live, _ = await session_store.create(operator_id)

    page = await session_store.list_operator_conversations(operator_id, cursor=None, limit=10)
    rows = {conversation.conversation_id: conversation for conversation in page.conversations}

    failed_row = rows[await session_store.conversation_of(failed.session_id)]
    assert failed_row.live_session is None
    assert failed_row.last_session_status == SessionStatus.FAILED
    replaced_row = rows[await session_store.conversation_of(replaced.session_id)]
    assert replaced_row.live_session is not None
    assert replaced_row.last_session_status is None
    live_row = rows[await session_store.conversation_of(live.session_id)]
    assert live_row.live_session is not None
    assert live_row.last_session_status is None


async def test_a_conversation_created_between_two_pages_cannot_shift_what_the_second_one_holds(
    session_store, operator_id
) -> None:
    """A conversation never ends, so this order only grows and only at its top."""
    older, _ = await session_store.create(operator_id)
    newer, _ = await session_store.create(operator_id)

    first = await session_store.list_operator_conversations(operator_id, cursor=None, limit=1)
    await session_store.create(operator_id)
    second = await session_store.list_operator_conversations(operator_id, cursor=first.next_cursor, limit=1)

    assert [conversation.conversation_id for conversation in first.conversations] == [
        await session_store.conversation_of(newer.session_id)
    ]
    assert [conversation.conversation_id for conversation in second.conversations] == [
        await session_store.conversation_of(older.session_id)
    ]


async def test_the_last_page_of_conversations_offers_no_cursor(session_store, operator_id) -> None:
    await session_store.create(operator_id)

    page = await session_store.list_operator_conversations(operator_id, cursor=None, limit=10)

    assert len(page.conversations) == 1
    assert page.next_cursor is None


async def test_a_second_prompt_is_refused_while_a_turn_is_open(session_store, operator_id) -> None:
    """Admission asks the turn rather than the session's status, so a mid-turn prompt cannot become
    fold-into-turn with no fold path wired."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "first", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None

    with pytest.raises(PromptRefusedError) as refusal:
        await session_store.enqueue_prompt(operator_id, view.session_id, "second", SPA_ORIGIN)
    assert refusal.value.reason is PromptRejection.TURN_IN_FLIGHT

    await session_store.end_turn(turn.turn_id, TurnAnswered())
    await session_store.enqueue_prompt(operator_id, view.session_id, "second", SPA_ORIGIN)


async def test_a_prompt_is_taken_off_the_queue_rather_than_found_by_status(
    session_store, migrated_sessions, operator_id
) -> None:
    """The queue row is what says a prompt is waiting, and claiming it is what says it no longer is
    — the item's own status cannot mean that, since a prompt is complete the moment it is accepted.

    Keyed by the conversation, so a prompt may outlive the session that took it."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)

    conversation_id = await session_store.conversation_of(view.session_id)
    async with migrated_sessions() as db:
        queued = list(await db.scalars(select(ConversationPrompt)))
    assert [(row.conversation_id, row.claimed_at) for row in queued] == [(conversation_id, None)]

    turn = await session_store.next_prompt(view.session_id)

    assert turn is not None
    assert turn.prompt == "why did it fail?", "the text comes from the item the queue names"
    async with migrated_sessions() as db:
        [claimed] = list(await db.scalars(select(ConversationPrompt)))
    assert (claimed.claimed_at is not None, claimed.claimed_by_session_id) == (True, view.session_id)
    assert claimed.item_id == turn.item_id


async def test_one_prompt_in_flight_is_a_schema_property(session_store, migrated_sessions, operator_id) -> None:
    """The index and not a scan-plus-rule: two replicas racing on one session would otherwise each
    conclude they may accept."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "first", SPA_ORIGIN)

    conversation_id = await session_store.conversation_of(view.session_id)
    async with migrated_sessions() as db:
        item = ConversationItem(
            item_id=uuid4(),
            conversation_id=conversation_id,
            session_id=view.session_id,
            item_type=ItemType.PROMPT,
            status=ItemStatus.COMPLETE,
            opened_seq=100,
            closed_seq=102,
            item_text="second",
            origin=SPA_ORIGIN,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(item)
        await db.flush()
        db.add(
            ConversationPrompt(
                prompt_id=uuid4(), conversation_id=conversation_id, item_id=item.item_id, queued_at=datetime.now(UTC)
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_a_prompt_item_with_no_queue_row_is_not_a_prompt(session_store, migrated_sessions, operator_id) -> None:
    """The queue is the only admission record. A prompt item on its own is transcript — one already
    answered, or the residue of a session that was stuck — not a prompt waiting to run."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await session_store.conversation_of(view.session_id)
    async with migrated_sessions.begin() as db:
        db.add(
            ConversationItem(
                item_id=uuid4(),
                conversation_id=conversation_id,
                session_id=view.session_id,
                item_type=ItemType.PROMPT,
                status=ItemStatus.COMPLETE,
                opened_seq=100,
                closed_seq=102,
                item_text="an item nothing queued",
                origin=SPA_ORIGIN,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    assert await session_store.next_prompt(view.session_id) is None
    # And it does not block a real one.
    await session_store.enqueue_prompt(operator_id, view.session_id, "mine", SPA_ORIGIN)
    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    assert turn.prompt == "mine"


async def test_the_view_says_responding_for_as_long_as_the_turn_is_open(session_store, operator_id) -> None:
    """`status` is the SPA's contract, and the view derives it from the open turn rather than from
    the column."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "work", SPA_ORIGIN)
    assert (await session_store.get(operator_id, view.session_id)).status == SessionStatus.READY, (
        "a queued prompt is not a turn in flight"
    )

    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    assert (await session_store.get(operator_id, view.session_id)).status == SessionStatus.RESPONDING
    assert await session_store.status(view.session_id) == SessionStatus.READY, (
        "the column itself no longer carries turn state"
    )

    await session_store.end_turn(turn.turn_id, TurnAnswered())
    assert (await session_store.get(operator_id, view.session_id)).status == SessionStatus.READY


async def test_a_session_that_ended_does_not_report_a_turn_it_left_open(
    session_store, migrated_sessions, operator_id
) -> None:
    """A replica losing its pod mid-turn closes nothing, so the open row is exactly the record of
    an abandoned exchange — and must not make a failed session read as still working."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "work", SPA_ORIGIN)
    assert await session_store.next_prompt(view.session_id) is not None
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await session_store.expire_stale_leases() == 1

    [record] = await session_store.list_turns(str(view.session_id), cursor=None, limit=10, scope=UnrestrictedReads())
    assert record.ended_at is None, "nothing ran to close it, and the record should say so"
    assert (await session_store.get(operator_id, view.session_id)).status == SessionStatus.FAILED


async def test_abort_is_refused_until_a_turn_is_actually_running(session_store, operator_id) -> None:
    """An idle session has nothing to interrupt, and saying so is the point of the 409.

    A *queued* prompt is not a turn either. An abort accepted with nothing to abort leaves its
    event set until the next turn, killing that one on arrival — so the abort names the open turn,
    which does not exist until the prompt is handed to the model.
    """
    view, token = await session_store.create(operator_id)
    # The bridge handshake is what takes a session from provisioning to ready, and only a
    # ready session accepts a prompt.
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await session_store.request_abort(view.session_id) is False

    await session_store.enqueue_prompt(operator_id, view.session_id, "work", SPA_ORIGIN)
    assert await session_store.request_abort(view.session_id) is False, "a queued prompt is not a turn"

    turn = await session_store.next_prompt(view.session_id)
    assert turn is not None
    assert await session_store.request_abort(view.session_id) is True

    await session_store.end_turn(turn.turn_id, TurnAnswered())
    assert await session_store.request_abort(view.session_id) is False


async def test_abort_reaches_the_replica_running_the_turn(
    migrated_db_url, session_store, session_wakes, operator_id
) -> None:
    """The two ends of an abort are on different pods, so it has to cross the database: the abort
    event belongs to whichever replica holds the runner's bridge websocket, while `POST .../abort`
    is balanced across all of them. Two stores over two engines is what reproduces that; a single
    store would pass on an in-process path.
    """
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "work", SPA_ORIGIN)
    assert await session_store.next_prompt(view.session_id) is not None, "the turn the abort names"

    other_engine = create_async_engine(migrated_db_url, pool_pre_ping=True)
    try:
        requesting = Store(
            async_sessionmaker(other_engine, expire_on_commit=False),
            RuntimeRegistry({RuntimeKind.CLAUDE_CODE: cast(RuntimeAdapter, _AlternateFrameVocabulary())}),
        )
        received: asyncio.Queue[SessionEvent] = asyncio.Queue()
        with session_wakes.watch_session(view.session_id, received.put_nowait):
            assert await requesting.request_abort(view.session_id) is True
            async with asyncio.timeout(30):
                # Drained, not first: delivery is commit-ordered, so the update notifies committed
                # by the setup above may still be in flight when the watch begins and then land
                # ahead of the abort. The contract is that the abort arrives.
                while (await received.get()).kind is not SessionEventKind.ABORT:
                    pass
    finally:
        await other_engine.dispose()


async def test_a_session_opens_its_own_conversation_unless_it_is_given_one(
    session_store, migrated_sessions, operator_id
) -> None:
    """The identity a channel's attachment hangs off. A caller with a thread to continue names it,
    and a caller with none — every session the browser starts — gets one of its own."""
    first, _ = await session_store.create(operator_id)
    second, _ = await session_store.create(operator_id)

    async with migrated_sessions() as db:
        opened = (await db.get(Session, first.session_id)).conversation_id
        assert opened != (await db.get(Session, second.session_id)).conversation_id
        assert (await db.get(Conversation, opened)).operator_id == operator_id

    continued, _ = await session_store.create(operator_id, conversation_id=opened)

    async with migrated_sessions() as db:
        assert (await db.get(Session, continued.session_id)).conversation_id == opened
        assert (await db.get(Conversation, opened)).runtime_kind == "claude_code"


async def _items(migrated_sessions, session_id: UUID) -> list[UUID]:
    """This session's items, oldest first."""
    async with migrated_sessions() as db:
        return list(
            await db.scalars(
                select(ConversationItem.item_id)
                .where(ConversationItem.session_id == session_id)
                .order_by(ConversationItem.opened_seq)
            )
        )


async def item_events(migrated_sessions, session_id: UUID) -> list[ConversationEventRow]:
    """Every row of *session_id*'s stream that is about an item, oldest first."""
    async with migrated_sessions() as db:
        return list(
            await db.scalars(
                select(ConversationEventRow)
                .where(ConversationEventRow.session_id == session_id, ConversationEventRow.item_id.isnot(None))
                .order_by(ConversationEventRow.event_seq)
            )
        )


async def authored_events(migrated_sessions, session_id: UUID) -> list[ConversationEventRow]:
    """Every row of *session_id*'s stream that is about the session rather than about an item.

    The authored arm alone is not the distinction any more: a prompt is authored too, because it is
    accepted before anything crosses a wire.
    """
    async with migrated_sessions() as db:
        return list(
            await db.scalars(
                select(ConversationEventRow)
                .where(ConversationEventRow.session_id == session_id, ConversationEventRow.item_id.is_(None))
                .order_by(ConversationEventRow.event_seq)
            )
        )


async def authored_events_of_kind(
    migrated_sessions, session_id: UUID, kind: AuthoredEventKind
) -> list[ConversationEventRow]:
    return [event for event in await authored_events(migrated_sessions, session_id) if event.kind == kind]


async def test_creating_a_session_records_that_it_started_provisioning(
    session_store, migrated_sessions, operator_id
) -> None:
    view, _ = await session_store.create(operator_id)

    started = one(
        await authored_events_of_kind(migrated_sessions, view.session_id, AuthoredEventKind.SESSION_PROVISIONING)
    )
    assert started.body == {}
    assert (started.session_id, started.turn_id, started.source_first_frame_seq) == (view.session_id, None, None)


async def test_setup_narration_is_authored_into_the_conversation_record(
    session_store, migrated_sessions, operator_id
) -> None:
    view, _ = await session_store.create(operator_id)

    await session_store.narrate(view.session_id, "cloning haku-state")
    await session_store.narrate(view.session_id, "starting the runner")

    lines = await authored_events_of_kind(migrated_sessions, view.session_id, AuthoredEventKind.SETUP_NARRATION)
    assert [line.body for line in lines] == [{"text": "cloning haku-state"}, {"text": "starting the runner"}]


async def test_a_replica_taking_a_session_over_records_who_it_took_it_from(
    session_store, migrated_sessions, operator_id
) -> None:
    """The fact the frame log cannot hold: a lease changing hands crosses no wire, and it happens on
    every roll."""
    view, token = await session_store.create(operator_id)
    with patch("haku.console.session.store.REPLICA", "haku-console-b"):
        assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    taken = one(await authored_events_of_kind(migrated_sessions, view.session_id, AuthoredEventKind.SESSION_ADOPTED))
    assert taken.kind == AuthoredEventKind.SESSION_ADOPTED
    assert taken.body == {"previous_holder": "haku-console-b", "holder": REPLICA}
    # A fact about the session, not about an exchange — which is what makes it writable at all for
    # a session that never reached a turn, and what keeps re-projection from seeing it.
    assert (taken.turn_id, taken.source_first_frame_seq) == (None, None)


async def test_the_first_runner_to_attach_is_not_a_takeover_and_neither_is_its_redial(
    session_store, migrated_sessions, operator_id
) -> None:
    """A session being served for the first time changed no hands, and a socket dropping and
    redialling to the same replica changes none either. Recording those would make every session's
    stream open with an ownership event that says nothing happened."""
    view, token = await session_store.create(operator_id)

    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert [event.kind for event in await authored_events(migrated_sessions, view.session_id)] == [
        AuthoredEventKind.SESSION_PROVISIONING
    ]


async def test_a_session_that_died_before_a_runner_ever_attached_says_so_in_a_row(
    session_store, migrated_sessions, operator_id
) -> None:
    """The case with nothing else to show: no frames and no turn. The reason is recorded rather than
    parsed back out of the operator-facing prose, because the sweep decides it from two columns the
    failure then overwrites.
    """
    view, _ = await session_store.create(operator_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await session_store.expire_stale_leases() == 1

    lapsed = one(await authored_events_of_kind(migrated_sessions, view.session_id, AuthoredEventKind.LEASE_EXPIRED))
    assert lapsed.kind == AuthoredEventKind.LEASE_EXPIRED
    assert lapsed.body == {"reason": LeaseExpiryReason.NEVER_ATTACHED, "last_holder": None}
    assert lapsed.turn_id is None
    ended = one(await authored_events_of_kind(migrated_sessions, view.session_id, AuthoredEventKind.SESSION_ENDED))
    assert ended.body == {"status": SessionStatus.FAILED, "error": "console session ended: a runner never attached"}


async def test_a_lease_that_lapsed_names_the_replica_that_held_it(
    session_store, migrated_sessions, operator_id
) -> None:
    """A different reason and a different answer to "who was serving this", from the same sweep."""
    view, _ = await session_store.create(operator_id)
    await session_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await session_store.expire_stale_leases() == 1

    lapsed = one(await authored_events_of_kind(migrated_sessions, view.session_id, AuthoredEventKind.LEASE_EXPIRED))
    assert lapsed.body == {"reason": LeaseExpiryReason.HOLDER_GONE, "last_holder": REPLICA}


@pytest.fixture
async def accepted_prompt(session_store: Store, operator_id: UUID) -> tuple[UUID, UUID]:
    """A ready session with one prompt it has accepted, and no turn yet claiming it.

    Room-backed because the tests using it are about what a channel reads back — but a room is an
    attachment address here, not a homeserver.
    """
    view, token = await session_store.create(operator_id)
    assert token is not None
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    prompt = await session_store.enqueue_prompt(
        operator_id, view.session_id, "what were we doing", MatrixOrigin(address=ROOM, refs=("$asked",))
    )
    return view.session_id, prompt


async def test_an_accepted_prompt_is_an_item_like_any_other(session_store, migrated_sessions, operator_id) -> None:
    """The operator's own question, opened, spoken and closed in one breath — its whole text is
    known when it is accepted, so it has exactly one segment and no window in which it is open.

    Addressed by `event_seq` like the agent's answer is, because without it a reader following the
    stream sees answers to questions that are not in it."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    item_id = await session_store.enqueue_prompt(operator_id, view.session_id, "list the files", SPA_ORIGIN)

    asked, said, closed = await item_events(migrated_sessions, view.session_id)
    assert [row.kind for row in (asked, said, closed)] == [
        ConversationEventKind.ITEM_OPENED,
        ConversationEventKind.ITEM_SEGMENT,
        ConversationEventKind.ITEM_COMPLETED,
    ]
    assert {row.item_id for row in (asked, said, closed)} == {item_id}
    assert asked.body == {"item_type": "prompt", "origin": {"kind": "spa"}}
    assert said.body == {"text": "list the files"}
    # No frames because nothing has been sent yet, and no turn because admission refuses a prompt
    # while one is open — so a prompt is accepted exactly when there is none to name.
    assert (asked.turn_id, asked.source_first_frame_seq) == (None, None)


async def test_an_aborted_turn_is_a_row_in_the_stream_and_names_its_turn(
    session_store, migrated_sessions, accepted_prompt
) -> None:
    """The operator's stop, in the ordered stream a channel reads rather than only in a column.

    An abort is a turn's **outcome** rather than an event of its own — which is where every backend
    protocol puts it — so what a reader folding the stream sees is the exchange ending, with the
    reason on it, and the room's "aborted" line is that row rendered.
    """
    session_id, _ = accepted_prompt
    turn = await session_store.next_prompt(session_id)
    assert turn is not None

    await session_store.end_turn(turn.turn_id, TurnAborted())

    stopped = one(
        event
        for event in await authored_events(migrated_sessions, session_id)
        if event.kind == AuthoredEventKind.TURN_ENDED
    )
    assert (stopped.turn_id, stopped.body) == (turn.turn_id, {"outcome": TurnOutcome.ABORTED})


async def test_a_turn_that_ended_any_other_way_leaves_no_abort_row(
    session_store, migrated_sessions, accepted_prompt
) -> None:
    """A turn that answered was not stopped, and a second close cannot re-decide that — the same
    early return that keeps the first outcome keeps a second row from being minted after it."""
    session_id, _ = accepted_prompt
    turn = await session_store.next_prompt(session_id)
    assert turn is not None

    await session_store.end_turn(turn.turn_id, TurnAnswered())
    await session_store.end_turn(turn.turn_id, TurnAborted())

    outcomes = [
        event.body["outcome"]
        for event in await authored_events(migrated_sessions, session_id)
        if event.kind == AuthoredEventKind.TURN_ENDED
    ]
    assert outcomes == [TurnOutcome.ANSWERED]


async def test_a_refused_prompt_is_not_in_the_stream(session_store, migrated_sessions, operator_id) -> None:
    """The row and the event commit together, so what is not accepted is not recorded."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "first", SPA_ORIGIN)

    with pytest.raises(PromptRefusedError) as refusal:
        await session_store.enqueue_prompt(operator_id, view.session_id, "second", SPA_ORIGIN)
    assert refusal.value.reason is PromptRejection.PROMPT_QUEUED

    said = one(
        row
        for row in await item_events(migrated_sessions, view.session_id)
        if row.kind == ConversationEventKind.ITEM_SEGMENT
    )
    assert said.body["text"] == "first"


async def test_a_live_session_whose_holder_stopped_renewing_is_failed(
    session_store, migrated_sessions, operator_id
) -> None:
    """A live status nobody is working on. A replica that dies without running its finalizer
    corrects nothing and every other observer reads the status it left as healthy, so the room is
    never answered and never told why; the expired lease is what makes it reclaimable by anyone.
    """
    view, _ = await session_store.create(operator_id)
    await session_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await session_store.expire_stale_leases() == 1
    assert await session_store.status(view.session_id) == SessionStatus.FAILED
    assert "went away" in (await session_store.get(operator_id, view.session_id)).error


async def test_a_session_is_adoptable_before_it_is_dead(session_store, migrated_sessions, operator_id) -> None:
    """`release_lease` is a finalizer, so SIGKILL and node loss skip it, and failing the row the
    moment the lease lapses beats the runner's redial every time — killing the session while its
    sandbox sits there retrying. An expired lease has to mean unowned for long enough to be
    taken."""
    view, _ = await session_store.create(operator_id)
    await session_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await session_store.expire_stale_leases() == 0, "expired is adoptable, not dead"
    assert await session_store.status(view.session_id) in OPEN_SESSION_STATUSES

    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await session_store.expire_stale_leases() == 1, "and dead once nobody took it"


async def test_shutdown_hands_back_every_lease_this_replica_holds(
    session_store, migrated_sessions, operator_id
) -> None:
    """The graceful-shutdown path: a rolling replica releases all its live sessions in one act, so
    each is adoptable at once instead of waiting out the sweep's grace. Not failed — handed back."""
    held = [await session_store.create(operator_id) for _ in range(2)]
    for view, token in held:
        assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await session_store.release_held_leases() == 2

    for view, _ in held:
        holder, expires_at = await lease_of(migrated_sessions, view.session_id)
        assert holder is None
        assert expires_at is not None
        assert expires_at <= datetime.now(UTC), "the lease is expired, so any runner may adopt it"
        assert await session_store.status(view.session_id) in OPEN_SESSION_STATUSES, "adoptable, not failed"
    assert await session_store.expire_stale_leases() == 0, "within the grace, so no sweep fails it yet"


async def test_shutdown_leaves_another_replicas_lease_alone(session_store, migrated_sessions, operator_id) -> None:
    """One replica going down must not hand back a session another replica is still serving."""
    view, token = await session_store.create(operator_id)
    with patch("haku.console.session.store.REPLICA", "haku-console-b"):
        assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await session_store.release_held_leases() == 0
    holder, _ = await lease_of(migrated_sessions, view.session_id)
    assert holder == "haku-console-b"


async def test_shutdown_does_not_touch_an_ended_session(session_store, migrated_sessions, operator_id) -> None:
    """A session that already ended is not this replica's to hand back, even if it once held it."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.fail(view.session_id, "something went wrong")

    assert await session_store.release_held_leases() == 0
    assert (await session_store.get(operator_id, view.session_id)).status == SessionStatus.FAILED


async def test_an_unheld_session_says_no_replica_ever_attached(session_store, migrated_sessions, operator_id) -> None:
    """The creator's provisioning grant has no holder, so a sandbox that never came up must not
    blame a replica for going away."""
    view, _ = await session_store.create(operator_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await session_store.expire_stale_leases() == 1
    assert "never attached" in (await session_store.get(operator_id, view.session_id)).error


async def test_a_failed_session_names_the_replica_that_held_it(session_store, migrated_sessions, operator_id) -> None:
    """The reason to record a holder: without it a room says a session died and nothing says which
    process to go read."""
    view, _ = await session_store.create(operator_id)
    await session_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await session_store.expire_stale_leases() == 1
    assert REPLICA in (await session_store.get(operator_id, view.session_id)).error


async def test_renewing_is_what_claims_the_session(session_store, migrated_sessions, operator_id) -> None:
    """A session goes from budgeted to held the first time its replica renews, with nothing else
    sequencing the handover."""
    view, _ = await session_store.create(operator_id)
    async with migrated_sessions() as db:
        assert (await db.get(Session, view.session_id)).lease_holder is None

    await session_store.renew_lease(view.session_id)

    async with migrated_sessions() as db:
        assert (await db.get(Session, view.session_id)).lease_holder == REPLICA


async def test_a_session_whose_holder_is_still_renewing_is_left_alone(
    session_store, migrated_sessions, operator_id
) -> None:
    """A busy replica must not have its session reclaimed out from under it."""
    view, _ = await session_store.create(operator_id)
    await session_store.renew_lease(view.session_id)

    assert await session_store.expire_stale_leases() == 0
    assert await session_store.status(view.session_id) == SessionStatus.PROVISIONING


async def test_an_ended_session_is_not_reclassified_by_the_sweep(session_store, migrated_sessions, operator_id) -> None:
    """Only a *live* status is a lie worth correcting; a terminal one is already the truth."""
    view, _ = await session_store.create(operator_id)
    await session_store.fail(view.session_id, "something else went wrong first")
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await session_store.expire_stale_leases() == 0
    assert (await session_store.get(operator_id, view.session_id)).error == "something else went wrong first"


async def test_a_frames_events_land_as_rows_with_the_cursor_that_says_they_did(
    session_store, migrated_sessions, operator_id
) -> None:
    """The projection's own output, stored in the transaction that moves the cursor."""
    view, token = await session_store.create(operator_id)
    session_id = view.session_id
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, session_id, "list the files", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    await session_store.apply_frame(
        session_id,
        started.turn_id,
        7,
        [
            ToolCallStarted(
                call_id="toolu_1", tool_name="Bash", arguments={"command": "ls"}, provenance=FrameRange(7, 7)
            ),
            MessageStarted(provenance=FrameRange(7, 7)),
            ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text="looking", provenance=FrameRange(7, 7)),
            MessageCompleted(backend_item_id=None, provenance=FrameRange(7, 7)),
        ],
    )
    await session_store.apply_frame(
        session_id,
        started.turn_id,
        8,
        # A result comes back on its own frame, and finds its call by the id rather than by
        # anything the message said.
        [
            ItemSegment(item=CallRef(call_id="toolu_1"), text="a.py", provenance=FrameRange(8, 8)),
            ToolCallCompleted(
                item=CallRef(call_id="toolu_1"),
                structured={"exit_code": 0},
                outcome=ToolOutcome.SUCCEEDED,
                provenance=FrameRange(8, 8),
            ),
        ],
    )

    async with migrated_sessions() as db:
        rows = list(
            (
                await db.scalars(
                    select(ConversationEventRow)
                    .where(ConversationEventRow.session_id == session_id)
                    .order_by(ConversationEventRow.event_seq)
                )
            ).all()
        )
        assert (await db.get(Session, session_id)).projected_frame_seq == 8
    assert [row.kind for row in rows if row.provenance is EventProvenance.FRAME_RANGE] == [
        ConversationEventKind.ITEM_OPENED,  # the call
        ConversationEventKind.ITEM_OPENED,  # the message
        ConversationEventKind.ITEM_SEGMENT,
        ConversationEventKind.ITEM_COMPLETED,
        ConversationEventKind.ITEM_SEGMENT,  # what the call printed
        ConversationEventKind.ITEM_COMPLETED,
    ]
    assert {row.turn_id for row in rows if row.provenance is EventProvenance.FRAME_RANGE} == {started.turn_id}
    # The result's two rows found the call by its id, frames after the ask, and landed on the same
    # item as the ask itself.
    asked = one(row for row in rows if row.body.get("call_id") == "toolu_1")
    answered = rows[-2:]
    assert {row.item_id for row in answered} == {asked.item_id}
    assert answered[0].body == {"text": "a.py"}


async def test_an_event_row_cannot_be_written_without_a_provenance_union(
    session_store, migrated_sessions, operator_id
) -> None:
    """Either arm is writable and neither can be written half: `frame_range` without a range, and
    `authored` with one, are both refused by the table rather than by whoever remembers. The turn
    and the item go the same way — required of a projected row, since the fold only runs inside a
    turn and only ever produces item rows, and absent on the facts the console authors about the
    session itself.

    **Which arm a row may take does not follow from its kind.** An item kind takes either — a
    prompt is authored, an assistant message is folded — so what the kind states is only whether an
    item is named at all, and `conversation_item.item_type` is where the arm actually follows from.
    """
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, view.session_id, "list the files", SPA_ORIGIN)
    started = await session_store.next_prompt(view.session_id)
    assert started is not None

    conversation_id = await session_store.conversation_of(view.session_id)
    item_id = one(await _items(migrated_sessions, view.session_id))
    seq = itertools.count(1_000)

    def event(**overrides) -> ConversationEventRow:
        values = {
            "conversation_id": conversation_id,
            "event_seq": next(seq),
            "session_id": view.session_id,
            "turn_id": started.turn_id,
            "item_id": item_id,
            "kind": ConversationEventKind.ITEM_OPENED,
            "provenance": EventProvenance.FRAME_RANGE,
            "source_first_frame_seq": 3,
            "source_last_frame_seq": 4,
            "body": {"item_type": "reasoning"},
            "created_at": datetime.now(UTC),
        }
        return ConversationEventRow(**(values | overrides))

    for unwritable in (
        event(source_first_frame_seq=None, source_last_frame_seq=None),
        event(source_last_frame_seq=None),
        event(provenance=EventProvenance.AUTHORED),
        event(source_first_frame_seq=9),
        event(turn_id=None),
        # An item kind names an item and the rest do not: `ck_conversation_event_item_kinds`.
        event(item_id=None),
        event(kind=AuthoredEventKind.SESSION_ADOPTED, body={"previous_holder": None, "holder": "a"}),
    ):
        async with migrated_sessions() as db:
            db.add(unwritable)
            with pytest.raises(IntegrityError):
                await db.commit()

    authored = {
        "kind": AuthoredEventKind.SESSION_ADOPTED,
        "item_id": None,
        "provenance": EventProvenance.AUTHORED,
        "source_first_frame_seq": None,
        "source_last_frame_seq": None,
        "body": {"previous_holder": None, "holder": "haku-console-a"},
    }
    # And the arm an item kind may still take: a prompt is an item the console authored, so
    # "folded from frames" is not what an item kind means.
    for writable in (
        event(**authored),
        event(**authored, turn_id=None),
        event(provenance=EventProvenance.AUTHORED, source_first_frame_seq=None, source_last_frame_seq=None),
    ):
        async with migrated_sessions() as db:
            db.add(writable)
            await db.commit()


async def _exchange(session_store, operator_id, session_id: UUID, prompt: str, answer: str) -> None:
    """One prompt through to one finished answer, with the frames it took, as the loop writes them."""
    await session_store.enqueue_prompt(operator_id, session_id, prompt, SPA_ORIGIN)
    turn = await session_store.next_prompt(session_id)
    assert turn is not None
    await session_store.record_frame(
        session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"}
    )
    spoke = await session_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "assistant"}
    )
    await session_store.apply_frame(
        session_id,
        turn.turn_id,
        spoke.frame_seq,
        [
            MessageStarted(provenance=FrameRange(spoke.frame_seq, spoke.frame_seq)),
            ItemSegment(
                item=OpenRef(item_type=ItemType.MESSAGE),
                text=answer,
                provenance=FrameRange(spoke.frame_seq, spoke.frame_seq),
            ),
            MessageCompleted(backend_item_id=None, provenance=FrameRange(spoke.frame_seq, spoke.frame_seq)),
        ],
    )
    await session_store.end_turn(turn.turn_id, TurnAnswered(), last_frame_seq=spoke.frame_seq)


async def test_an_update_carries_the_rows_the_events_after_a_position_name(session_store, operator_id) -> None:
    """What the update drops is the history — the part that grows without bound.

    A reader holding the first exchange's position is sent the second exchange and not the first,
    which is the whole of what an update is for.
    """
    view, token = await session_store.create(operator_id)
    session_id = view.session_id
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await session_store.conversation_of(session_id)
    await _exchange(session_store, operator_id, session_id, "first", "one")
    held = await session_store.conversation_position(conversation_id)

    await _exchange(session_store, operator_id, session_id, "second", "two")
    changes = await session_store.read_operator_conversation_changes(operator_id, conversation_id, after=held, limit=50)

    assert [(entry.kind, entry.text) for entry in changes.entries if isinstance(entry, PromptEntry | MessageEntry)] == [
        ("prompt", "second"),
        ("message", "two"),
    ]
    assert changes.position > held
    # Re-reading the same position is the same answer: the merge replaces by `opened_seq`, so a
    # duplicate costs nothing and nothing about delivery has to be exactly-once.
    again = await session_store.read_operator_conversation_changes(operator_id, conversation_id, after=held, limit=50)
    assert [entry.opened_seq for entry in again.entries] == [entry.opened_seq for entry in changes.entries]


async def test_an_update_carries_what_a_replaced_session_wrote_after_the_position(session_store, operator_id) -> None:
    """The position addresses the thread, so it survives the runner under it being replaced: rows
    the old session wrote after it are still owed to the reader, and the new session's follow them.
    """
    first, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(first.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await session_store.conversation_of(first.session_id)
    held = await session_store.conversation_position(conversation_id)
    await _exchange(session_store, operator_id, first.session_id, "before the sandbox died", "answered")

    second, token = await session_store.create(operator_id, conversation_id=conversation_id)
    assert await session_store.authenticate_bridge(second.session_id, token) == BridgeAuthentication.ACCEPTED
    await _exchange(session_store, operator_id, second.session_id, "after it was replaced", "answered again")
    changes = await session_store.read_operator_conversation_changes(operator_id, conversation_id, after=held, limit=50)

    assert [entry.text for entry in changes.entries if isinstance(entry, PromptEntry | MessageEntry)] == [
        "before the sandbox died",
        "answered",
        "after it was replaced",
        "answered again",
    ]
    assert changes.session_id == second.session_id


async def test_a_claimed_prompt_reaches_a_reader_as_the_responding_status(session_store, operator_id) -> None:
    """`next_prompt` takes the operator's question off the queue and touches no item row; what
    tells a tab the thread started working is the session's derived status, and the exchange
    itself is `list_turns`' business."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await session_store.conversation_of(view.session_id)
    await session_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    enqueued = await session_store.read_operator_conversation_changes(operator_id, conversation_id, after=0, limit=50)
    assert [entry.kind for entry in enqueued.entries] == ["prompt"]
    asked = one(entry for entry in enqueued.entries if isinstance(entry, PromptEntry))
    assert asked.text == "why did it fail?"

    assert await session_store.next_prompt(view.session_id) is not None
    claimed = await session_store.read_operator_conversation_changes(
        operator_id, conversation_id, after=enqueued.position, limit=50
    )

    assert claimed.entries == []
    assert claimed.status == SessionStatus.RESPONDING


async def test_a_position_the_log_cannot_answer_from_is_refused_rather_than_read_as_empty(
    session_store, operator_id
) -> None:
    """ "Nothing after N" and "N is not in this log" are different answers, and only one of them is
    safe to serve. `event_seq` is a global `Identity`, so the difference cannot be a comparison: the
    positions a read hands out are 0 and this conversation's own rows, so membership is the check.
    """
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await session_store.conversation_of(view.session_id)
    await session_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    held = await session_store.conversation_position(conversation_id)

    with pytest.raises(PositionUnusableError):
        await session_store.read_operator_conversation_changes(operator_id, conversation_id, after=held + 1, limit=50)


async def test_an_update_over_its_limit_is_refused_rather_than_shortened(session_store, operator_id) -> None:
    """Silently short is a message the reader never learns about. `ConversationFollow` turns the
    refusal into a snapshot, which is the honest answer when most of one would be sent anyway."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await session_store.conversation_of(view.session_id)
    await _exchange(session_store, operator_id, view.session_id, "first", "one")
    await _exchange(session_store, operator_id, view.session_id, "second", "two")

    with pytest.raises(PositionUnusableError):
        await session_store.read_operator_conversation_changes(operator_id, conversation_id, after=0, limit=2)

    whole = await session_store.read_operator_conversation_changes(operator_id, conversation_id, after=0, limit=50)
    # Two exchanges of two rows each: the prompt and the answer.
    assert len(whole.entries) == 4


async def test_the_update_refuses_a_conversation_another_operator_owns(session_store, operator_id) -> None:
    """The MCP reader is deliberately unscoped (R5.3a); a browser surface must never be."""
    view, _ = await session_store.create(operator_id)

    with pytest.raises(KeyError):
        await session_store.read_operator_conversation_changes(
            uuid4(), await session_store.conversation_of(view.session_id), after=0, limit=50
        )


async def test_open_wake_turn_brackets_a_harness_initiated_exchange(
    session_store, migrated_sessions, operator_id
) -> None:
    """The harness began an exchange itself, so the store opens the bracket after the fact: a turn
    anchored on the exchange's first recorded frame, and a prompt item in the harness's voice
    saying what woke it."""
    view, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    wake = await session_store.open_wake_turn(
        view.session_id, 'Background command "fetch" completed', first_frame_seq=7
    )

    assert wake is not None
    async with migrated_sessions() as db:
        turn = await db.get(ConversationTurn, wake.turn_id)
        assert turn is not None
        assert turn.first_frame_seq == 7
        session_row = await db.get(Session, view.session_id)
        assert session_row is not None
        assert session_row.projected_frame_seq == 6
    entries = await _conversation_entries(session_store, await session_store.conversation_of(view.session_id))
    prompt = one(entry for entry in entries if isinstance(entry, PromptEntry))
    assert prompt.text == 'Background command "fetch" completed'
    assert prompt.origin == PromptOriginKind.HARNESS


async def test_open_wake_turn_refuses_a_session_that_ended(session_store, operator_id) -> None:
    """A wake frame can race the session's end; the bracket must not reopen a dead session."""
    view, _token = await session_store.create(operator_id)
    await session_store.fail(view.session_id, "the sandbox went away")
    assert await session_store.open_wake_turn(view.session_id, "too late", first_frame_seq=1) is None


if __name__ == "__main__":
    pytest_bazel.main()

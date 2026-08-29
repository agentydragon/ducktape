"""End-to-end contracts for `get_worker_result` over the real store and a migrated Postgres.

The tool is exercised the way the orchestrator Agent reaches it: through `build_mcp`'s real
`haku_conversations` server, over a real `ConversationReads` on a migrated Postgres, reading worker
sessions a real `Store` wrote. Nothing the tool touches is stood in for — the store, the session
status derivation, the profile-DAG read scope, and the durable Agent authority are all real; only
the sandbox a worker would run in is absent, which a status/answer read never consults.

The worker Agent is a real `public-coder` Agent (minted through the authority) so its conversation
pins a real profile the orchestrator's read closure reaches; the callers are the real MCP execution
principals, synthesised the way the outer boundary hands them in.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from fastmcp import Client, FastMCP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conftest import console_sessions, operator_identity_store
from haku.console.conversation.conversation_event import TurnOutcome
from haku.console.conversation.item_vocabulary import ItemStatus, ItemType
from haku.console.conversation.reader import ConversationReads
from haku.console.conversation.reads import WorkerResult, WorkerStatus
from haku.console.conversation_read_access import ConversationReadAccessPolicy
from haku.console.database_schema import Conversation, ConversationItem, ConversationTurn, Session
from haku.console.grants.principal import RequestPrincipal
from haku.console.harnesses.kind import HarnessKind
from haku.console.identity.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.mcp.execution import AgentMcpExecutionCaller, McpExecutionContext, mcp_execution_request_meta
from haku.console.mcp.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp_config import AccessProfile
from haku.console.session.status import SessionStatus
from haku.console.session.store import Store
from haku.console.tools.conversations import HAKU_CONVERSATIONS_SERVER_ID, build_mcp

# The orchestrator fans work onto a worker; its read closure reaches the worker's profile, an
# outsider's reaches neither. The deployed graph is `haku` → `public-coder`; the names are reused so
# the test reads against the same shapes.
_ORCHESTRATOR = "haku"
_WORKER_PROFILE = "public-coder"
_OUTSIDER = "outsider"
_PROFILES = (
    AccessProfile(
        id=_ORCHESTRATOR,
        auto_approval_policy="manual",
        in_process_server_ids={HAKU_CONVERSATIONS_SERVER_ID},
        can_read_profiles={_WORKER_PROFILE},
    ),
    AccessProfile(id=_WORKER_PROFILE, auto_approval_policy="manual"),
    # Reaches the server but reads only its own profile — the fence a worker's result must respect.
    AccessProfile(id=_OUTSIDER, auto_approval_policy="manual", in_process_server_ids={HAKU_CONVERSATIONS_SERVER_ID}),
)
_ACCESS = InProcessServerAccessPolicy(_PROFILES)
_READS = ConversationReadAccessPolicy(_PROFILES)

_WORKER_AGENT = UUID("40000000-0000-4000-8000-00000000cc01")
_ORCHESTRATOR_AGENT = UUID("40000000-0000-4000-8000-00000000cc02")
_FAR_FUTURE = datetime.datetime(2999, 1, 1, tzinfo=datetime.UTC)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@dataclass(frozen=True, slots=True)
class _Env:
    sessions: async_sessionmaker[AsyncSession]
    store: Store
    mcp: FastMCP
    operator_id: UUID
    worker_agent_id: UUID


@pytest.fixture
async def env(migrated_db_url: str) -> _Env:
    sessions = console_sessions(migrated_db_url)
    identity_store = operator_identity_store(migrated_db_url)
    operator_id = await identity_store.resolve_configured_external_user_key("worker-op")
    authority = PostgresAgentAuthority(
        sessions,
        public_base_url="https://haku.test",
        operator_identity_store=identity_store,
        access_profiles=(_ORCHESTRATOR, _WORKER_PROFILE, _OUTSIDER),
        default_access_profile_id=_WORKER_PROFILE,
    )
    await authority.reconcile_static_agents(
        [
            StaticAgentDefinition(
                agent_id=_WORKER_AGENT,
                display_name="Public Coder",
                operator_id=operator_id,
                secret_reference="env:HAKU_CONSOLE_TEST_WORKER_TOKEN",
                token_fingerprint=fingerprint_static_token("worker-result-token"),
                access_profile_id=_WORKER_PROFILE,
            )
        ]
    )
    store = Store(sessions)
    return _Env(
        sessions=sessions,
        store=store,
        mcp=build_mcp(ConversationReads(store), access=_ACCESS, conversation_reads=_READS),
        operator_id=operator_id,
        worker_agent_id=_WORKER_AGENT,
    )


async def _seed_session(env: _Env, *, ready: bool, profile: str = _WORKER_PROFILE) -> tuple[UUID, UUID]:
    """A worker conversation and its session, `ready` (a live runner) or idle (dispatched, unstarted)."""
    now = _now()
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
        # `ready` is these facts: an allocated credential, an attached runner, a live lease.
        live = (
            {"bridge_token_fingerprint": session_id.bytes, "bridge_connected_at": now, "lease_expires_at": _FAR_FUTURE}
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


async def _seed_turn(
    env: _Env, conversation_id: UUID, session_id: UUID, *, outcome: TurnOutcome | None, failure: str | None = None
) -> None:
    now = _now()
    ended = outcome is not None
    async with env.sessions.begin() as db:
        db.add(
            ConversationTurn(
                turn_id=uuid4(),
                conversation_id=conversation_id,
                session_id=session_id,
                first_seq=1,
                last_seq=2 if ended else None,
                started_at=now,
                ended_at=now if ended else None,
                outcome=outcome,
                failure=failure,
            )
        )


async def _seed_message(env: _Env, conversation_id: UUID, session_id: UUID, text: str) -> None:
    now = _now()
    async with env.sessions.begin() as db:
        db.add(
            ConversationItem(
                item_id=uuid4(),
                conversation_id=conversation_id,
                session_id=session_id,
                item_type=ItemType.MESSAGE,
                status=ItemStatus.COMPLETE,
                opened_seq=3,
                closed_seq=4,
                item_text=text,
                created_at=now,
                updated_at=now,
            )
        )


def _meta(profile: str) -> dict[str, object]:
    caller = AgentMcpExecutionCaller(
        principal=RequestPrincipal(agent_id=_ORCHESTRATOR_AGENT, session_id=None, access_profile_id=profile)
    )
    return mcp_execution_request_meta(
        McpExecutionContext(caller=caller, tool_call_id="tc_test", approving_operator_id=None, approval_policy_id=None)
    )


async def _call(env: _Env, session_id: UUID, *, profile: str = _ORCHESTRATOR, raise_on_error: bool = False):
    async with Client(env.mcp) as client:
        return await client.call_tool(
            "get_worker_result", {"session_id": str(session_id)}, meta=_meta(profile), raise_on_error=raise_on_error
        )


async def _result(env: _Env, session_id: UUID, *, profile: str = _ORCHESTRATOR) -> WorkerResult:
    return WorkerResult.model_validate((await _call(env, session_id, profile=profile)).structured_content)


async def test_a_dispatched_worker_with_no_turn_yet_reports_running(env: _Env) -> None:
    session_id, _ = await _seed_session(env, ready=False)

    result = await _result(env, session_id)

    assert result.status is WorkerStatus.RUNNING
    assert result.result is None


async def test_a_worker_mid_turn_reports_running_and_withholds_its_answer(env: _Env) -> None:
    session_id, conversation_id = await _seed_session(env, ready=True)
    await _seed_turn(env, conversation_id, session_id, outcome=None)

    result = await _result(env, session_id)

    assert result.status is WorkerStatus.RUNNING
    assert result.result is None


async def test_a_worker_that_answered_reports_done_with_its_final_message(env: _Env) -> None:
    """`done` follows the answered turn, not a closed session: a one-shot worker stays ready after
    it answers, and the orchestrator must still read its result off the still-live session."""
    session_id, conversation_id = await _seed_session(env, ready=True)
    await _seed_turn(env, conversation_id, session_id, outcome=TurnOutcome.ANSWERED)
    await _seed_message(env, conversation_id, session_id, "Opened the PR: https://example.test/pr/7")

    # The point of the case: the session is still live, yet the worker is reported done.
    assert await env.store.status(session_id) is SessionStatus.READY
    result = await _result(env, session_id)

    assert result.status is WorkerStatus.DONE
    assert result.result == "Opened the PR: https://example.test/pr/7"


async def test_a_failed_session_reports_failed_with_its_error_surface(env: _Env) -> None:
    session_id, _ = await _seed_session(env, ready=True)
    await env.store.fail(session_id, "sandbox runner disconnected")

    result = await _result(env, session_id)

    assert result.status is WorkerStatus.FAILED
    assert result.result == "sandbox runner disconnected"


async def test_a_failed_turn_reports_failed_with_the_turn_failure(env: _Env) -> None:
    """A turn can die without the session dying, so its failure is the surface even while the
    session itself is still ready."""
    session_id, conversation_id = await _seed_session(env, ready=True)
    await _seed_turn(
        env, conversation_id, session_id, outcome=TurnOutcome.FAILED, failure="the model returned an error"
    )

    result = await _result(env, session_id)

    assert result.status is WorkerStatus.FAILED
    assert result.result == "the model returned an error"


async def test_a_session_outside_the_read_scope_is_refused(env: _Env) -> None:
    """The worker's result is fenced by the same profile-DAG scope the other reads use: a caller
    whose closure does not reach the worker's profile is denied, not handed the answer."""
    session_id, conversation_id = await _seed_session(env, ready=True)
    await _seed_turn(env, conversation_id, session_id, outcome=TurnOutcome.ANSWERED)
    await _seed_message(env, conversation_id, session_id, "secret")

    result = await _call(env, session_id, profile=_OUTSIDER, raise_on_error=False)

    assert result.is_error
    assert "conversation access denied" in str(result.content)


async def test_an_unknown_session_is_refused(env: _Env) -> None:
    result = await _call(env, uuid4(), raise_on_error=False)

    assert result.is_error
    assert "worker session not found" in str(result.content)


if __name__ == "__main__":
    pytest_bazel.main()

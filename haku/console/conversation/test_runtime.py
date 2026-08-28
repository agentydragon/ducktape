"""Durable conversation demand creates and replaces sessions without a channel supervisor."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.conftest import console_sessions
from haku.console.conversation.prompt_origin import SPA_ORIGIN
from haku.console.conversation.runtime import Runtime
from haku.console.database_schema import ConversationItem, Session
from haku.console.harnesses.kind import HarnessKind
from haku.console.identity.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.session.conftest import configured_runtimes
from haku.console.session.launch_identity import ChatLaunchAuthorizer, LaunchIdentity
from haku.console.session.runtime import SessionService
from haku.console.session.status import SessionStatus
from haku.console.session.store import ADOPTION_GRACE, BridgeAuthentication, Store
from haku.console.x.runtime import RuntimeKey


async def test_first_conversation_prompt_creates_one_session_then_one_sandbox(
    session_store,
    chat_service,
    allocator,
    conversation_wakes,
    migrated_engine,
    migrated_sessions,
    operator_id,
    recording_claims,
) -> None:
    view, _ = await session_store.create_idle(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    # Model a conversation created or attached without a runtime session.
    async with migrated_sessions.begin() as db:
        await db.delete(await db.get(Session, view.session_id))

    item_id = await session_store.enqueue_conversation_prompt(operator_id, conversation_id, "start", SPA_ORIGIN)
    runtime = Runtime(chat_service, session_store, conversation_wakes, migrated_engine)

    await runtime.reconcile_once()
    await runtime.reconcile_once()

    async with migrated_sessions() as db:
        sessions = list(await db.scalars(select(Session).where(Session.conversation_id == conversation_id)))
        item = await db.get(ConversationItem, item_id)
    assert len(sessions) == 1
    assert sessions[0].status == SessionStatus.IDLE
    assert item is not None
    assert item.session_id == sessions[0].session_id
    assert recording_claims.created == []

    await allocator.allocate_once()

    assert recording_claims.created == [sessions[0].session_id]
    assert await session_store.status(sessions[0].session_id) == SessionStatus.PROVISIONING


async def test_demanded_replacement_reauthorizes_pinned_identity_in_creation_transaction(
    migrated_db_url,
    migrated_sessions,
    migrated_identity_store,
    session_wakes,
    conversation_wakes,
    migrated_engine,
    operator_id,
    recording_claims,
) -> None:
    expected_agent_id = uuid4()
    authority = PostgresAgentAuthority(
        console_sessions(migrated_db_url),
        public_base_url="https://haku.test",
        operator_identity_store=migrated_identity_store,
        access_profiles=("pinned",),
        default_access_profile_id="pinned",
    )
    await authority.reconcile_static_agents(
        [
            StaticAgentDefinition(
                agent_id=expected_agent_id,
                display_name="Demanded Replacement Agent",
                operator_id=operator_id,
                secret_reference="env:DEMANDED_REPLACEMENT_AGENT",
                token_fingerprint=fingerprint_static_token("demanded-replacement-token"),
                access_profile_id="pinned",
            )
        ]
    )
    production = ChatLaunchAuthorizer(
        authority,
        launchable_agent_ids={expected_agent_id},
        registered_runtime_identities={RuntimeKey(expected_agent_id, HarnessKind.CLAUDE_CODE)},
        profile_runtime_kinds={"pinned": {HarnessKind.CLAUDE_CODE}},
    )
    calls: list[tuple[UUID, str | None, bool]] = []

    async def authorize(
        db: AsyncSession,
        operator_id: UUID,
        agent_id: UUID,
        runtime_kind: HarnessKind,
        *,
        expected_profile_id: str | None = None,
    ) -> LaunchIdentity:
        assert db.in_transaction()
        assert agent_id == expected_agent_id
        calls.append((operator_id, expected_profile_id, db.in_transaction()))
        return await production(db, operator_id, agent_id, runtime_kind, expected_profile_id=expected_profile_id)

    runtimes = configured_runtimes(recording_claims)
    store = Store(migrated_sessions)
    service = SessionService(
        runtimes, store, session_wakes, launch_authorizer=authorize, default_agent_id=expected_agent_id
    )
    first = await service.create(operator_id)
    conversation_id = await store.conversation_of(first.session_id)
    async with migrated_sessions.begin() as db:
        await db.delete(await db.get(Session, first.session_id))
    await store.enqueue_conversation_prompt(operator_id, conversation_id, "replace", SPA_ORIGIN)

    await Runtime(service, store, conversation_wakes, migrated_engine).reconcile_once()

    async with migrated_sessions() as db:
        replacements = list(await db.scalars(select(Session).where(Session.conversation_id == conversation_id)))
    assert len(replacements) == 1
    assert replacements[0].status is SessionStatus.IDLE
    assert calls == [(operator_id, None, True), (operator_id, "pinned", True)]


async def test_new_runtime_and_rolling_old_creator_converge_on_one_session(
    session_store, chat_service, conversation_wakes, migrated_engine, migrated_sessions, operator_id
) -> None:
    view, _ = await session_store.create_idle(operator_id)
    conversation_id = await session_store.conversation_of(view.session_id)
    async with migrated_sessions.begin() as db:
        await db.delete(await db.get(Session, view.session_id))
    await session_store.enqueue_conversation_prompt(operator_id, conversation_id, "once", SPA_ORIGIN)
    first = Runtime(chat_service, session_store, conversation_wakes, migrated_engine)
    second = Runtime(chat_service, session_store, conversation_wakes, migrated_engine)

    await asyncio.gather(
        first.reconcile_once(),
        second.reconcile_once(),
        session_store.create_idle(operator_id, conversation_id=conversation_id),
    )

    async with migrated_sessions() as db:
        count = await db.scalar(
            select(func.count()).select_from(Session).where(Session.conversation_id == conversation_id)
        )
    assert count == 1


async def test_unclaimed_prompt_moves_to_replacement_after_a_stale_lease(
    session_store, chat_service, conversation_wakes, migrated_engine, migrated_sessions, operator_id, recording_claims
) -> None:
    first, token = await session_store.create(operator_id)
    assert await session_store.authenticate_bridge(first.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await session_store.conversation_of(first.session_id)
    item_id = await session_store.enqueue_conversation_prompt(
        operator_id, conversation_id, "do not lose me", SPA_ORIGIN
    )
    async with migrated_sessions.begin() as db:
        row = await db.get(Session, first.session_id)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - ADOPTION_GRACE - timedelta(seconds=1)

    await Runtime(chat_service, session_store, conversation_wakes, migrated_engine).reconcile_once()

    async with migrated_sessions() as db:
        sessions = list(
            await db.scalars(
                select(Session)
                .where(Session.conversation_id == conversation_id)
                .order_by(Session.created_at, Session.session_id)
            )
        )
        item = await db.get(ConversationItem, item_id)
    assert [row.status for row in sessions] == [SessionStatus.FAILED, SessionStatus.IDLE]
    assert item is not None
    assert item.session_id == sessions[1].session_id
    assert first.session_id in recording_claims.deleted


if __name__ == "__main__":
    pytest_bazel.main()

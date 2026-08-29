"""Durable, channel-neutral reconciliation of first-prompt sandbox demand."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest_bazel

from haku.console.conversation.prompt_origin import SPA_ORIGIN
from haku.console.harnesses.kind import HarnessKind
from haku.console.session.conftest import TEST_AGENT_ID
from haku.console.session.sandbox_allocation import SandboxAllocator
from haku.console.session.status import SessionStatus


async def test_an_untouched_session_stays_idle(
    allocator, chat_service, session_store, recording_claims, operator_id
) -> None:
    view = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)

    await allocator.allocate_once()

    assert recording_claims.created == []
    assert await session_store.status(view.session_id) == SessionStatus.IDLE


async def test_a_prompt_commit_survives_before_any_allocator_runs(
    allocator, chat_service, session_store, recording_claims, operator_id
) -> None:
    """A replica may die after admission; a later sweep reads the durable demand and allocates."""
    view = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)
    await chat_service.enqueue_prompt(operator_id, view.session_id, "wake up", SPA_ORIGIN)

    assert recording_claims.created == []
    assert await session_store.status(view.session_id) == SessionStatus.IDLE

    await allocator.allocate_once()

    assert recording_claims.created == [view.session_id]
    assert await session_store.status(view.session_id) == SessionStatus.PROVISIONING


async def test_a_new_allocator_recovers_demand_left_by_a_stopped_replica(
    chat_service, session_store, session_wakes, recording_claims, migrated_engine, operator_id
) -> None:
    view = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)
    await chat_service.enqueue_prompt(operator_id, view.session_id, "still here", SPA_ORIGIN)
    restarted = SandboxAllocator(chat_service, session_store, session_wakes, migrated_engine)

    await restarted.allocate_once()

    assert recording_claims.created == [view.session_id]


async def test_competing_allocator_passes_create_exactly_one_claim(
    allocator, chat_service, recording_claims, operator_id
) -> None:
    view = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)
    await chat_service.enqueue_prompt(operator_id, view.session_id, "one sandbox", SPA_ORIGIN)

    await asyncio.gather(allocator.allocate_once(), allocator.allocate_once())

    assert recording_claims.created == [view.session_id]


async def test_allocator_is_compatible_with_the_previous_request_path_during_rollout(
    allocator, chat_service, recording_claims, operator_id
) -> None:
    """An old replica may still allocate inline while a new SBOX leader sees the same demand."""
    view = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)
    await chat_service.enqueue_prompt(operator_id, view.session_id, "during the roll", SPA_ORIGIN)

    await asyncio.gather(allocator.allocate_once(), chat_service.allocate(operator_id, view.session_id))

    assert recording_claims.created == [view.session_id]


async def test_prompt_notification_wakes_one_elected_allocator(
    allocator, chat_service, session_store, session_wakes, recording_claims, migrated_engine, operator_id
) -> None:
    """Two live replicas watch, one holds SBOX, and demand does not wait for the 10-second sweep."""
    other = SandboxAllocator(chat_service, session_store, session_wakes, migrated_engine)
    view = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)
    first_sweep = asyncio.Event()
    sessions_awaiting_sandbox = session_store.sessions_awaiting_sandbox

    async def observed_sweep():
        demands = await sessions_awaiting_sandbox()
        first_sweep.set()
        return demands

    with patch.object(session_store, "sessions_awaiting_sandbox", observed_sweep):
        async with allocator.run(), other.run():
            # Finish the elected replica's startup pass with no demand. The allocation below must
            # therefore come from the PROMPT wake, not that initial pass or the periodic backstop.
            async with asyncio.timeout(3):
                await first_sweep.wait()
            await chat_service.enqueue_prompt(operator_id, view.session_id, "wake the leader", SPA_ORIGIN)
            async with asyncio.timeout(3):
                await recording_claims.created_event.wait()

    assert recording_claims.created == [view.session_id]
    assert await session_store.status(view.session_id) == SessionStatus.PROVISIONING


async def test_one_failed_claim_does_not_stop_unrelated_demand(
    allocator, chat_service, session_store, recording_claims, operator_id
) -> None:
    refused = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)
    served = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)
    for view in (refused, served):
        await chat_service.enqueue_prompt(operator_id, view.session_id, "start", SPA_ORIGIN)
    recording_claims.refuse(refused.session_id)

    await allocator.allocate_once()

    assert recording_claims.created == [served.session_id]
    assert await session_store.status(refused.session_id) == SessionStatus.FAILED
    assert refused.session_id in recording_claims.deleted


async def test_a_replacement_idle_session_inherits_unclaimed_conversation_demand(
    allocator, chat_service, session_store, recording_claims, operator_id
) -> None:
    first = await chat_service.create(operator_id, agent_id=TEST_AGENT_ID, harness_kind=HarnessKind.CLAUDE_CODE)
    await chat_service.enqueue_prompt(operator_id, first.session_id, "do not lose me", SPA_ORIGIN)
    conversation_id = await session_store.conversation_of(first.session_id)
    await session_store.fail(first.session_id, "replica stopped before allocation")
    replacement = await chat_service.create(operator_id, conversation_id=conversation_id)

    await allocator.allocate_once()

    assert recording_claims.created == [replacement.session_id]
    assert await session_store.status(replacement.session_id) == SessionStatus.PROVISIONING


if __name__ == "__main__":
    pytest_bazel.main()

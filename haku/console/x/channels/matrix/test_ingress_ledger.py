"""What the record already carries, and what it still owes an answer for.

Written against the real stores, because both questions are about rows other writers leave behind:
`carrying` has to commit inside `enqueue_prompt`'s transaction, and `unanswered` is a join over a
prompt queue and a session status nothing here sets by hand.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel

from haku.console.x.channels.matrix.ingress_ledger import IngressLedger, Unanswered
from haku.console.x.session_store import BridgeAuthentication, MatrixSession, PromptRefusedError, SessionStore

ROOM = "!room:allegedly.works"


async def ready_session(chat_store: SessionStore, operator_id: UUID) -> UUID:
    """A Matrix session that will take a prompt, made the way the supervisor and a runner make one."""
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    return view.session_id


@pytest.fixture
async def session_id(chat_store: SessionStore, operator_id: UUID) -> UUID:
    return await ready_session(chat_store, operator_id)


async def test_an_event_is_carried_from_the_moment_its_prompt_commits(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    await chat_store.enqueue_prompt(operator_id, session_id, "[$a] hi", ledger.carrying(("$a",)))

    assert await ledger.carried(["$a", "$b"]) == frozenset({"$a"})


async def test_a_refused_prompt_carries_nothing(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    """The rows and the prompt are one transaction, so a prompt admission refuses records nothing —
    which is what leaves the homeserver free to offer the batch again."""
    await chat_store.enqueue_prompt(operator_id, session_id, "[$a] hi", ledger.carrying(("$a",)))

    with pytest.raises(PromptRefusedError):
        await chat_store.enqueue_prompt(operator_id, session_id, "[$b] and this", ledger.carrying(("$b",)))

    assert await ledger.carried(["$b"]) == frozenset()


async def test_a_queued_prompt_on_a_live_session_is_not_unanswered(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    """Work in hand, not work lost: the harness will take this one, and offering it again would be
    the duplicate the ledger exists to prevent."""
    await chat_store.enqueue_prompt(operator_id, session_id, "[$a] hi", ledger.carrying(("$a",)))

    assert await ledger.unanswered() is None


async def test_a_prompt_whose_session_ended_before_claiming_it_is_unanswered(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    await chat_store.enqueue_prompt(operator_id, session_id, "[$a] hi", ledger.carrying(("$a", "$b")))
    await chat_store.closed(session_id)

    assert await ledger.unanswered() == Unanswered(text="[$a] hi", event_ids=("$a", "$b"))


async def test_a_prompt_the_session_claimed_before_ending_is_answered_for(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    """Claimed is where this channel's promise ends. A turn that then failed is the turn loop's
    business, and re-asking would put the operator's message back in front of an agent that has
    already read it."""
    await chat_store.enqueue_prompt(operator_id, session_id, "[$a] hi", ledger.carrying(("$a",)))
    assert await chat_store.next_prompt(session_id) is not None
    await chat_store.closed(session_id)

    assert await ledger.unanswered() is None


async def test_a_prompt_no_matrix_event_produced_is_not_this_channel_s_to_re_offer(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    """A prompt typed into the SPA is stranded by the same death and is not offered again here —
    the ledger's promise is about messages this channel accepted on the operator's behalf."""
    await chat_store.enqueue_prompt(operator_id, session_id, "typed in a tab")
    await chat_store.closed(session_id)

    assert await ledger.unanswered() is None


async def test_the_oldest_outstanding_message_is_the_one_offered_first(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID
) -> None:
    """Two dead sessions, so the operator is answered in the order they spoke."""
    first = await ready_session(chat_store, operator_id)
    await chat_store.enqueue_prompt(operator_id, first, "[$a] first", ledger.carrying(("$a",)))
    await chat_store.closed(first)
    second = await ready_session(chat_store, operator_id)
    await chat_store.enqueue_prompt(operator_id, second, "[$b] second", ledger.carrying(("$b",)))
    await chat_store.closed(second)

    assert await ledger.unanswered() == Unanswered(text="[$a] first", event_ids=("$a",))


async def test_re_recording_an_event_moves_it_to_the_prompt_now_answering_for_it(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    """The prompt this leaves behind is transcript. Nothing points at it, so nothing finds it
    outstanding again — which is what makes re-offering happen once rather than every pass."""
    await chat_store.enqueue_prompt(operator_id, session_id, "[$a] hi", ledger.carrying(("$a",)))
    await chat_store.closed(session_id)
    replacement = await ready_session(chat_store, operator_id)
    await chat_store.enqueue_prompt(operator_id, replacement, "[$a] hi", ledger.carrying(("$a",)))

    assert await ledger.unanswered() is None


if __name__ == "__main__":
    pytest_bazel.main()

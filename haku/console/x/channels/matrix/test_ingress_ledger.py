"""What the record already carries, and why nothing has to be offered again.

Written against the real stores, because the question is about rows another writer leaves behind:
`carrying` has to commit inside `enqueue_prompt`'s transaction, and what makes suppression safe is
that the prompt it suppresses for is one some session will run.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel

from haku.console.chat_models import MatrixOrigin
from haku.console.x.channels.matrix.ingress_ledger import IngressLedger
from haku.console.x.session_events import TurnAnsweredBody
from haku.console.x.session_store import BridgeAuthentication, PromptRefusedError, SessionStore

ROOM = "!room:allegedly.works"


def _from_room(*event_ids: str) -> MatrixOrigin:
    """The origin ingress mints for a batch, which is what every prompt here arrived as."""
    return MatrixOrigin(address=ROOM, refs=event_ids)


async def ready_session(chat_store: SessionStore, operator_id: UUID, *, conversation_id: UUID | None = None) -> UUID:
    """A Matrix session that will take a prompt, made the way the supervisor and a runner make one."""
    view, token = await chat_store.create(operator_id, conversation_id=conversation_id)
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    return view.session_id


@pytest.fixture
async def session_id(chat_store: SessionStore, operator_id: UUID) -> UUID:
    return await ready_session(chat_store, operator_id)


async def test_an_event_is_carried_from_the_moment_its_prompt_commits(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    await chat_store.enqueue_prompt(operator_id, session_id, "hi", _from_room("$a"), ledger.carrying(("$a",)))

    assert await ledger.carried(["$a", "$b"]) == frozenset({"$a"})


async def test_a_refused_prompt_carries_nothing(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    """The rows and the prompt are one transaction, so a prompt admission refuses records nothing —
    which is what leaves the homeserver free to offer the batch again."""
    await chat_store.enqueue_prompt(operator_id, session_id, "hi", _from_room("$a"), ledger.carrying(("$a",)))

    with pytest.raises(PromptRefusedError):
        await chat_store.enqueue_prompt(operator_id, session_id, "and this", _from_room("$b"), ledger.carrying(("$b",)))

    assert await ledger.carried(["$b"]) == frozenset()


async def test_a_prompt_its_session_never_claimed_is_taken_by_the_replacement(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID
) -> None:
    """Why suppressing a re-delivered event is safe, and why nothing re-offers.

    The queue belongs to the conversation, not to the session that accepted the prompt, so a
    sandbox dying between acceptance and the turn strands nothing: the replacement's own
    `next_prompt` finds the same row. What this replaces was a ledger query for stranded prompts and
    a channel that asked the live session the dead one's question — machinery for a window the
    schema no longer has.
    """
    view, _ = await chat_store.create(operator_id)
    conversation_id = await chat_store.conversation_of(view.session_id)
    first = await ready_session(chat_store, operator_id, conversation_id=conversation_id)
    await chat_store.enqueue_prompt(operator_id, first, "answer me", _from_room("$a"), ledger.carrying(("$a",)))
    await chat_store.closed(first)

    replacement = await ready_session(chat_store, operator_id, conversation_id=conversation_id)
    started = await chat_store.next_prompt(replacement)

    assert started is not None
    assert started.prompt == "answer me"
    # And the event is still carried, so the homeserver re-delivering it is dropped rather than
    # asked a second time.
    assert await ledger.carried(["$a"]) == frozenset({"$a"})


async def test_re_recording_an_event_moves_it_to_the_prompt_now_answering_for_it(
    ledger: IngressLedger, chat_store: SessionStore, operator_id: UUID, session_id: UUID
) -> None:
    """Two passes can race on one event, and the row is a pointer to whichever prompt answers for it
    rather than a claim about which was first."""
    await chat_store.enqueue_prompt(operator_id, session_id, "hi", _from_room("$a"), ledger.carrying(("$a",)))
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    await chat_store.end_turn(started.turn_id, TurnAnsweredBody())
    await chat_store.enqueue_prompt(operator_id, session_id, "hi again", _from_room("$a"), ledger.carrying(("$a",)))

    assert await ledger.carried(["$a"]) == frozenset({"$a"})


if __name__ == "__main__":
    pytest_bazel.main()

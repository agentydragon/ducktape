"""What `conversation.py` does with a room: bind its conversation, keep a session running under
it, and take what is said in it into a turn.

Ingress is here rather than beside the turn loop it feeds: `Turns.offer` takes homeserver
events and hands them to `submit_exclusive_prompt`, so a test of it is a test of the crossing. The turn loop's own admission rules are <../../x/test_session_runtime.py>, where no channel appears
at all. The conversation-history tests remain here beside the replacement-session setup that creates
their cross-session threads; the reader itself is channel-neutral.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.agents.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.channels.matrix.client import InboundMessage, UnmappableEvent
from haku.console.channels.matrix.conftest import MATRIX_CONFIG, MATRIX_OPERATOR, MATRIX_ROOM
from haku.console.channels.matrix.conversation import (
    ConversationFacts,
    ConversationStore,
    PromptAccepted,
    PromptRejected,
    RoomAttachment,
    Turns,
)
from haku.console.channels.matrix.ingress_ledger import IngressLedger
from haku.console.chat_models import ItemType, RuntimeKind
from haku.console.conftest import console_sessions
from haku.console.conversation import conversation_event
from haku.console.conversation.conversation_event import ConversationEventKind, FrameRange, PromptRejection
from haku.console.conversation.history import ConversationHistory
from haku.console.conversation.prompt_origin import SPA_ORIGIN, MatrixOrigin
from haku.console.database_schema import Conversation, ConversationEventRow, ConversationItem, Session, SubmittedPrompt
from haku.console.session.launch_identity import ChatLaunchAuthorizer, LaunchIdentity
from haku.console.session.store import BridgeAuthentication, Store
from haku.console.x.conversation_events import (
    ConversationEvent as FoldedEvent,
    ItemSegment,
    MessageCompleted,
    MessageStarted,
    OpenRef,
)
from haku.console.x.runtime import RuntimeKey


async def test_first_matrix_bind_pins_complete_identity_with_production_authorizer(
    migrated_db_url, migrated_sessions, migrated_identity_store, operator_id
) -> None:
    agent_id = uuid4()
    authority = PostgresAgentAuthority(
        console_sessions(migrated_db_url),
        public_base_url="https://haku.test",
        operator_identity_store=migrated_identity_store,
        access_profiles=("chat",),
        default_access_profile_id="chat",
    )
    await authority.reconcile_static_agents(
        [
            StaticAgentDefinition(
                agent_id=agent_id,
                display_name="Matrix Production Agent",
                operator_id=operator_id,
                secret_reference="env:MATRIX_PRODUCTION_AGENT",
                token_fingerprint=fingerprint_static_token("matrix-production-token"),
                access_profile_id="chat",
            )
        ]
    )
    production = ChatLaunchAuthorizer(
        authority,
        launchable_agent_ids={agent_id},
        registered_runtime_identities={RuntimeKey(agent_id, RuntimeKind.CLAUDE_CODE)},
        profile_runtime_kinds={"chat": {RuntimeKind.CLAUDE_CODE}},
    )
    calls: list[bool] = []

    async def authorize(
        db: AsyncSession,
        operator_id: UUID,
        agent_id: UUID,
        runtime_kind: RuntimeKind,
        *,
        expected_profile_id: str | None = None,
    ) -> LaunchIdentity:
        assert db.in_transaction()
        calls.append(db.in_transaction())
        return await production(db, operator_id, agent_id, runtime_kind, expected_profile_id=expected_profile_id)

    conversations = ConversationStore(migrated_sessions, launch_authorizer=authorize, default_agent_id=agent_id)
    bound = await conversations.bind_room(MATRIX_ROOM, operator_id)

    async with migrated_sessions() as db:
        conversation = await db.get(Conversation, bound.conversation_id)
    assert conversation is not None
    assert (conversation.operator_id, conversation.agent_id, conversation.access_profile_id) == (
        operator_id,
        agent_id,
        "chat",
    )
    assert conversation.runtime_kind is RuntimeKind.CLAUDE_CODE
    assert calls == [True]


@pytest.fixture
def transcript(migrated_sessions) -> ConversationHistory:
    return ConversationHistory(migrated_sessions)


@pytest.fixture
async def binding(conversations: ConversationStore, operator_id: UUID) -> RoomAttachment:
    """The room's live binding, made the way an invite makes it."""
    return await conversations.bind_room(MATRIX_ROOM, operator_id)


@pytest.fixture
def thread(binding: RoomAttachment) -> UUID:
    """The conversation the room holds a copy of."""
    return binding.conversation_id


async def another_thread(conversations: ConversationStore, operator_id: UUID) -> UUID:
    """A second conversation, bound the way a second invited room binds one."""
    return (await conversations.bind_room("!second:allegedly.works", operator_id)).conversation_id


async def serving_session(session_store: Store, operator_id: UUID, conversation_id: UUID) -> UUID:
    """A Matrix session ready to take prompts, made the way the supervisor and a runner make one."""
    view, token = await session_store.create(operator_id, conversation_id=conversation_id)
    assert token is not None
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    return view.session_id


async def exchange(session_store: Store, operator_id: UUID, session_id: UUID, asked: str, answered: str) -> None:
    """One question and its answer, written by the paths that write them in production.

    Not hand-inserted rows: this read depends on what the real writers leave behind — a prompt item
    the store opens and closes at admission, and a message item whose text is the segments the fold
    appended to it.
    """
    await session_store.enqueue_prompt(operator_id, session_id, asked, SPA_ORIGIN)
    start = await session_store.next_prompt(session_id)
    assert start is not None
    await say(session_store, session_id, start.turn_id, answered)
    # Ended, because admission asks about the turn: a session left mid-turn refuses the next
    # prompt, and these tests are conversations rather than one exchange each.
    await session_store.end_turn(start.turn_id, conversation_event.TurnAnswered())


async def say(session_store: Store, session_id: UUID, turn_id: UUID, answered: str, *, complete: bool = True) -> None:
    """One agent message, through the fold's own vocabulary."""
    where = FrameRange(1, 1)
    events: list[FoldedEvent] = [MessageStarted(provenance=where)]
    if answered:
        events.append(ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text=answered, provenance=where))
    if complete:
        events.append(MessageCompleted(backend_item_id=None, provenance=where))
    await session_store.apply_frame(session_id, turn_id, 1, events)


async def read(transcript: ConversationHistory, conversation_id: UUID) -> list[tuple[ItemType, str]]:
    """A thread's recent conversation as a replacement session that owns none of it would read it."""
    return [
        (message.item_type, message.body)
        for message in await transcript.recent(conversation_id, before_session=uuid4(), limit=20)
    ]


async def test_the_transcript_is_both_sides_of_the_conversation_in_order(
    transcript: ConversationHistory, session_store: Store, operator_id: UUID, thread: UUID
) -> None:
    session_id = await serving_session(session_store, operator_id, thread)

    await exchange(session_store, operator_id, session_id, "hi", "hello")
    await exchange(session_store, operator_id, session_id, "still there?", "yes")

    assert await read(transcript, thread) == [
        (ItemType.PROMPT, "hi"),
        (ItemType.MESSAGE, "hello"),
        (ItemType.PROMPT, "still there?"),
        (ItemType.MESSAGE, "yes"),
    ]


async def test_the_transcript_spans_every_session_of_the_thread(
    transcript: ConversationHistory, session_store: Store, operator_id: UUID, thread: UUID
) -> None:
    """The point of reading by conversation: the session that holds the context is the one gone.

    Sessions of one thread share `conversation_id`, so a replacement reads what its predecessor
    said without either of them being named.
    """
    first = await serving_session(session_store, operator_id, thread)
    await exchange(session_store, operator_id, first, "hi", "hello")
    await session_store.fail(first, "the sandbox went away")
    second = await serving_session(session_store, operator_id, thread)
    await exchange(session_store, operator_id, second, "again", "still here")

    assert await read(transcript, thread) == [
        (ItemType.PROMPT, "hi"),
        (ItemType.MESSAGE, "hello"),
        (ItemType.PROMPT, "again"),
        (ItemType.MESSAGE, "still here"),
    ]


async def test_a_batch_the_dying_session_never_answered_is_still_the_history(
    transcript: ConversationHistory, session_store: Store, operator_id: UUID, thread: UUID
) -> None:
    """What answers a message its session never got to: the replacement is handed it as context.

    The batch is acknowledged the moment it is accepted, so nothing offers it again — the prompt
    row ingress wrote is the whole of what survives, and this is the read that finds it.
    """
    doomed = await serving_session(session_store, operator_id, thread)
    await exchange(session_store, operator_id, doomed, "hi", "hello")
    # Accepted, and then nothing: no turn ever claimed it, which is what leaves it `pending`.
    await session_store.enqueue_prompt(operator_id, doomed, "the one that killed it", SPA_ORIGIN)
    await session_store.fail(doomed, "the sandbox went away")

    assert (ItemType.PROMPT, "the one that killed it") in await read(transcript, thread)


async def test_a_session_s_own_rows_are_not_its_history(
    transcript: ConversationHistory, session_store: Store, operator_id: UUID, thread: UUID
) -> None:
    """A prompt this session has already been handed is not also its history; twice is not context.

    The window is real: a session goes `ready` when its runner authenticates, and its system
    prompt is rendered a few statements later — so a batch can be accepted in between.
    """
    doomed = await serving_session(session_store, operator_id, thread)
    await exchange(session_store, operator_id, doomed, "hi", "hello")
    replacement = await serving_session(session_store, operator_id, thread)
    await session_store.enqueue_prompt(operator_id, replacement, "re-offered", SPA_ORIGIN)

    said = await transcript.recent(thread, before_session=replacement, limit=20)

    assert [(message.item_type, message.body) for message in said] == [
        (ItemType.PROMPT, "hi"),
        (ItemType.MESSAGE, "hello"),
    ]


async def test_what_the_room_was_never_told_is_not_in_the_history(
    transcript: ConversationHistory, session_store: Store, operator_id: UUID, thread: UUID
) -> None:
    """Haku's side is here on exactly the condition the room heard it on.

    Two items exist that were never an answer, and neither is context: one still being written into
    when its session died, and the empty one a turn that only ran tools leaves behind.
    """
    session_id = await serving_session(session_store, operator_id, thread)
    await session_store.enqueue_prompt(operator_id, session_id, "do something", SPA_ORIGIN)
    start = await session_store.next_prompt(session_id)
    assert start is not None
    await say(session_store, session_id, start.turn_id, "")
    await say(session_store, session_id, start.turn_id, "half an ans", complete=False)

    assert await read(transcript, thread) == [(ItemType.PROMPT, "do something")]


async def test_another_thread_is_not_this_thread(
    transcript: ConversationHistory,
    conversations: ConversationStore,
    session_store: Store,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """Threads are read apart: one bot holds several rooms, and each reads only its own."""
    elsewhere = await serving_session(session_store, operator_id, await another_thread(conversations, operator_id))

    await exchange(session_store, operator_id, elsewhere, "hi", "hello")

    assert await read(transcript, thread) == []


async def test_the_limit_takes_the_tail(
    transcript: ConversationHistory, session_store: Store, operator_id: UUID, thread: UUID
) -> None:
    session_id = await serving_session(session_store, operator_id, thread)
    await exchange(session_store, operator_id, session_id, "one", "re: one")
    await exchange(session_store, operator_id, session_id, "two", "re: two")

    said = await transcript.recent(thread, before_session=uuid4(), limit=2)

    assert [message.body for message in said] == ["two", "re: two"], "the newest, still oldest first"


@pytest.fixture
def turns(session_store: Store, migrated_identity_store, ledger: IngressLedger) -> Turns:
    """Ingress over the real stores — only the homeserver's events are handed in by the test."""
    return Turns(MATRIX_CONFIG, session_store, migrated_identity_store, ledger)


async def test_each_room_binds_its_own_conversation(
    conversations: ConversationStore, operator_id: UUID, binding: RoomAttachment
) -> None:
    """One bot serves many rooms: a second room binds beside the first, and re-binding a room is
    idempotent rather than a refusal."""
    second = await conversations.bind_room("!second:allegedly.works", operator_id)

    assert second.conversation_id != binding.conversation_id
    assert await conversations.bind_room(MATRIX_ROOM, operator_id) == binding
    assert await conversations.live_attachments() == (binding, second)


def operator_message(body: str, *, event_id: str, at: int) -> InboundMessage:
    """The operator saying *body* in the room, as `/sync` hands it over."""
    return InboundMessage(
        room_id=MATRIX_ROOM, event_id=event_id, sender=MATRIX_OPERATOR, body=body, origin_server_ts=at
    )


def _unmappable(msgtype: str) -> UnmappableEvent:
    return UnmappableEvent(room_id=MATRIX_ROOM, event_id=f"${msgtype}", sender=MATRIX_OPERATOR, msgtype=msgtype)


async def test_a_batch_a_ready_session_takes_becomes_its_prompt(
    turns: Turns, binding: RoomAttachment, session_store: Store, operator_id: UUID, thread: UUID
) -> None:
    """The accepted case, and what "one batch, one prompt" means: two events, one transcript row."""
    session_id = await serving_session(session_store, operator_id, thread)

    admitted = await turns.offer(
        binding, [operator_message("hi", event_id="$1", at=1), operator_message("and this", event_id="$2", at=2)]
    )

    assert isinstance(admitted, PromptAccepted)
    start = await session_store.next_prompt(session_id)
    assert start is not None
    assert start.prompt == "hi\nand this", "the ids ride on the prompt's own event now, not in its prose"


async def test_a_batch_offered_mid_turn_is_rejected_with_the_reason_and_the_text(
    turns: Turns, binding: RoomAttachment, session_store: Store, operator_id: UUID, thread: UUID
) -> None:
    """A message sent while Haku is working is answered rather than queued behind the turn, and the
    row it hands back is the only copy of what was said — the homeserver will not offer it again
    once the caller acknowledges the batch."""
    session_id = await serving_session(session_store, operator_id, thread)
    await session_store.enqueue_prompt(operator_id, session_id, "first", SPA_ORIGIN)
    assert await session_store.next_prompt(session_id) is not None

    admitted = await turns.offer(binding, [operator_message("and another thing", event_id="$2", at=2)])

    assert isinstance(admitted, PromptRejected)
    assert admitted.reason is PromptRejection.TURN_IN_FLIGHT
    assert (admitted.facts.conversation_id, admitted.facts.session_id) == (thread, None)
    assert admitted.facts.bodies == (
        conversation_event.PromptRejected(reason=PromptRejection.TURN_IN_FLIGHT, text="and another thing"),
    )


async def test_a_batch_offered_before_a_session_exists_becomes_conversation_demand(
    turns: Turns, binding: RoomAttachment, migrated_sessions, operator_id: UUID
) -> None:
    """Binding a room creates no session; its first prompt is still accepted durably."""
    admitted = await turns.offer(binding, [operator_message("hi", event_id="$1", at=1)])

    assert isinstance(admitted, PromptAccepted)
    async with migrated_sessions() as db:
        prompt = await db.get(SubmittedPrompt, admitted.prompt_id)
    assert prompt is not None
    # Pending in the inbox — owed to whichever session eventually admits it, bound to none yet.
    assert (prompt.conversation_id, prompt.text, prompt.admitted_at) == (binding.conversation_id, "hi", None)


async def test_a_batch_offered_after_a_session_is_gone_becomes_replacement_demand(
    turns: Turns, binding: RoomAttachment, session_store: Store, migrated_sessions, operator_id: UUID, thread: UUID
) -> None:
    """The room offers to its conversation, so a vanished session is not an ingress outage."""
    session_id = await serving_session(session_store, operator_id, thread)
    async with migrated_sessions.begin() as db:
        await db.execute(delete(Session).where(Session.session_id == session_id))

    admitted = await turns.offer(binding, [operator_message("hi", event_id="$1", at=1)])

    assert isinstance(admitted, PromptAccepted)


async def test_an_accepted_batch_records_its_events_against_the_prompt_it_became(
    turns: Turns, binding: RoomAttachment, session_store: Store, ledger: IngressLedger, operator_id: UUID, thread: UUID
) -> None:
    """The dedupe key, written where it cannot come apart from the prompt.

    A rejected batch records nothing, because there is no prompt for a row to name and the
    homeserver re-offering it is the outcome we want.
    """
    await serving_session(session_store, operator_id, thread)

    await turns.offer(
        binding, [operator_message("hi", event_id="$1", at=1), operator_message("more", event_id="$2", at=2)]
    )

    assert await ledger.carried(["$1", "$2", "$3"]) == frozenset({"$1", "$2"})


async def test_a_rejected_batch_records_nothing_for_the_homeserver_to_be_deduped_against(
    turns: Turns, binding: RoomAttachment, session_store: Store, ledger: IngressLedger, operator_id: UUID, thread: UUID
) -> None:
    session_id = await serving_session(session_store, operator_id, thread)
    await session_store.enqueue_prompt(operator_id, session_id, "first", SPA_ORIGIN)
    assert await session_store.next_prompt(session_id) is not None

    assert isinstance(await turns.offer(binding, [operator_message("hi", event_id="$1", at=1)]), PromptRejected)

    assert await ledger.carried(["$1"]) == frozenset()


async def test_a_prompt_its_session_never_answered_is_taken_by_the_replacement(
    turns: Turns, binding: RoomAttachment, session_store: Store, ledger: IngressLedger, operator_id: UUID, thread: UUID
) -> None:
    """Suppression is not acknowledgement: the batch was acknowledged to the homeserver, so this
    prompt is the only copy left of what the operator asked, and the session holding it died.

    Nothing re-offers it. The queue belongs to the conversation, so the replacement's own
    `next_prompt` finds the same row — what this replaces was a ledger query for stranded prompts
    and a channel that asked the live session the dead one's question.
    """
    doomed = await serving_session(session_store, operator_id, thread)
    await turns.offer(binding, [operator_message("did you see this", event_id="$1", at=1)])
    await session_store.closed(doomed)
    replacement = await serving_session(session_store, operator_id, thread)

    start = await session_store.next_prompt(replacement)

    assert start is not None
    assert start.prompt == "did you see this"
    assert await ledger.carried(["$1"]) == frozenset({"$1"}), "and the homeserver's re-delivery is still dropped"


async def test_an_unreadable_event_is_a_fact_per_event_on_the_live_conversation(
    turns: Turns, binding: RoomAttachment, session_store: Store, operator_id: UUID, thread: UUID
) -> None:
    """One fact per event, for the caller to append in the transaction that acknowledges the batch
    (the channel must derive it from the durable conversation record)."""
    await serving_session(session_store, operator_id, thread)

    facts = await turns.unreadable(binding, [_unmappable("m.image"), _unmappable("m.audio")])

    assert facts == ConversationFacts(
        conversation_id=thread,
        session_id=None,
        bodies=(
            conversation_event.UnreadableInput(media_type="m.image"),
            conversation_event.UnreadableInput(media_type="m.audio"),
        ),
    )


async def test_an_unreadable_event_with_no_session_behind_the_room_is_still_recorded(
    turns: Turns, binding: RoomAttachment
) -> None:
    """Same terms as a refusal: the conversation is what it is about, and it exists from the moment
    the room is bound."""
    facts = await turns.unreadable(binding, [_unmappable("m.image")])

    assert facts == ConversationFacts(
        conversation_id=binding.conversation_id,
        session_id=None,
        bodies=(conversation_event.UnreadableInput(media_type="m.image"),),
    )


async def test_a_batch_records_the_room_events_it_was_folded_from(
    turns: Turns, binding: RoomAttachment, session_store: Store, migrated_sessions, operator_id: UUID, thread: UUID
) -> None:
    """The prompt is what was said; which events said it rides on the prompt's own record, in the
    order they were folded. Nothing puts an event id in the text any more, so this is the
    only copy — and it names the room as well as the event, which is what a reader comparing
    origins needs while one bot serves more than one.
    """
    session_id = await serving_session(session_store, operator_id, thread)

    offered = await turns.offer(
        binding, [operator_message("first", event_id="$a", at=1), operator_message("second", event_id="$b", at=2)]
    )

    assert isinstance(offered, PromptAccepted)
    async with migrated_sessions() as db:
        accepted = await db.get(SubmittedPrompt, offered.prompt_id)
    assert accepted is not None
    assert (accepted.text, accepted.origin) == ("first\nsecond", MatrixOrigin(address=MATRIX_ROOM, refs=("$a", "$b")))
    # Admission materialises the item from that row, and the origin rides its opening event.
    started = await session_store.next_prompt(session_id)
    assert started is not None
    async with migrated_sessions() as db:
        prompt = await db.get(ConversationItem, started.item_id)
        assert prompt is not None
        assert (prompt.item_type, prompt.item_text) == (ItemType.PROMPT, "first\nsecond")
        asked = await db.scalar(
            select(ConversationEventRow).where(
                ConversationEventRow.item_id == started.item_id,
                ConversationEventRow.kind == ConversationEventKind.ITEM_OPENED,
            )
        )
    assert asked is not None
    assert conversation_event.PromptOpened.model_validate(asked.body).origin == MatrixOrigin(
        address=MATRIX_ROOM, refs=("$a", "$b")
    )


if __name__ == "__main__":
    pytest_bazel.main()

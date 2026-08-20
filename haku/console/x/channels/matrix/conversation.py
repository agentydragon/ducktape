"""The conversation the room Haku services is attached to.

A `chat_attachment` row binds the room to a conversation, and the conversation outlives every
session that runs under it. The binding and ingress are Matrix's; conversation history and turn
execution are channel-neutral, and a replacement joins the conversation the attachment already
names rather than re-pointing the attachment.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import ChatSurface, MatrixOrigin, PromptRejection, RuntimeKind
from haku.console.config import MatrixConfig
from haku.console.database_schema import ChatAttachment, Conversation, Operator
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x import session_events
from haku.console.x.channels.matrix.client import InboundMessage, UnmappableEvent
from haku.console.x.channels.matrix.ingress_ledger import IngressLedger
from haku.console.x.launch_identity import LaunchAuthorizer, LaunchIdentity
from haku.console.x.session_events import PromptRejectedBody, UnreadableInputBody
from haku.console.x.session_store import PromptRefusedError, SessionStore

logger = logging.getLogger(__name__)

# There is no row to lock before the first room bind. This transaction-scoped mutex keeps two
# ingress leaders from opening different conversations before the live-attachment uniqueness
# constraint can choose a winner.
_BIND_ADVISORY_LOCK = 0x4D58_4244  # "MXBD"


async def live_attachment(db: AsyncSession, room_id: str) -> UUID | None:
    """This room's live attachment, which is what its deliveries hang off.

    None where the room holds no conversation — a room this console never bound, or one detached
    since — in which case there is nothing to record a send against and the room notice is the only
    account of it.

    Takes the caller's session so a channel can record what it sent in the transaction that records
    the send itself (`outbox.RoomOutbox.mark_sent`).
    """
    attachment_id: UUID | None = await db.scalar(
        select(ChatAttachment.attachment_id).where(
            ChatAttachment.surface == ChatSurface.MATRIX,
            ChatAttachment.address == room_id,
            ChatAttachment.detached_at.is_(None),
        )
    )
    return attachment_id


@dataclass(frozen=True)
class BoundRoom:
    """The room this console services, and the conversation it holds a copy of."""

    room_id: str
    conversation_id: UUID


async def _live_binding(db: AsyncSession) -> BoundRoom | None:
    """The bound room, read off the attachment that is holding it.

    Ordered so that the answer is the room bound first. One room at a time is `bind_room`'s
    refusal rather than the schema's — `chat_attachment` deliberately admits many live rows,
    because one bot serving several rooms is where this goes — so a second row that somehow
    appeared must not make the bound room flip between reads.
    """
    row = (
        await db.execute(
            select(ChatAttachment.address, ChatAttachment.conversation_id)
            .where(ChatAttachment.surface == ChatSurface.MATRIX, ChatAttachment.detached_at.is_(None))
            .order_by(ChatAttachment.attached_at, ChatAttachment.attachment_id)
            .limit(1)
        )
    ).first()
    return None if row is None else BoundRoom(room_id=row.address, conversation_id=row.conversation_id)


class MatrixConversationStore:
    """Which durable conversation the bound room holds a copy of.

    Runtime sessions are deliberately absent from the binding. Neutral supervision creates and
    replaces them under the conversation, so the channel has no pointer to tend or re-aim.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        launch_authorizer: LaunchAuthorizer | None = None,
        default_agent_id: UUID | None = None,
    ):
        self._sessions = sessions
        self._launch_authorizer = launch_authorizer
        self._default_agent_id = default_agent_id

    def configure_launch_identity(self, authorizer: LaunchAuthorizer, *, default_agent_id: UUID) -> None:
        """Configure production first-bind identity selection after app composition."""
        self._launch_authorizer = authorizer
        self._default_agent_id = default_agent_id

    async def attachment(self, room_id: str) -> UUID | None:
        async with self._sessions() as db:
            return await live_attachment(db, room_id)

    async def bound_room(self) -> BoundRoom | None:
        """The room this console services, or None before the operator has invited it into one."""
        async with self._sessions() as db:
            return await _live_binding(db)

    async def bind_room(self, room_id: str, operator_id: UUID) -> BoundRoom:
        """Attach `room_id` if no room is attached yet; return whichever room is live.

        A caller that gets back a different room than it asked for has been refused. Binding opens
        the conversation the room holds a copy of, in the same transaction — so a bound room always
        has one, and the attachment outlives every session that serves it: a replacement joins the
        conversation the attachment already names instead of the attachment being re-pointed at it.

        Read-then-insert rather than insert-or-nothing, serialized by the sync loop's election:
        only its leader handles invites, so the read and the insert cannot interleave with another
        replica's. `uq_chat_attachment_live_address` is the backstop if that ever stops holding.
        """
        async with self._sessions() as db, db.begin():
            # There is no row to lock before the first bind, so serialize the empty-check with a
            # transaction advisory lock. The Operator row is also locked by the authorizer below;
            # both locks remain held through the conversation and attachment inserts.
            await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _BIND_ADVISORY_LOCK})
            await db.get(Operator, operator_id, with_for_update=True)
            if (live := await _live_binding(db)) is not None:
                return live
            now = datetime.datetime.now(datetime.UTC)
            identity: LaunchIdentity | None = None
            if self._launch_authorizer is not None:
                if self._default_agent_id is None:
                    raise RuntimeError("Matrix launch identity is not configured")
                identity = await self._launch_authorizer(
                    operator_id, self._default_agent_id, RuntimeKind.CLAUDE_CODE, db=db
                )
            conversation_id = uuid4()
            db.add(
                Conversation(
                    conversation_id=conversation_id,
                    operator_id=operator_id,
                    agent_id=None if identity is None else identity.agent_id,
                    access_profile_id=None if identity is None else identity.access_profile_id,
                    runtime_kind=RuntimeKind.CLAUDE_CODE if identity is None else identity.runtime_kind,
                    created_at=now,
                )
            )
            # Flushed before the attachment that points at it. The unit of work orders a flush from
            # `relationship()` dependencies and nothing else, so a bare `ForeignKey` between two
            # mappers leaves their inserts in mapper-name order — `chat_attachment` ahead of
            # `conversation`, which the constraint rejects.
            await db.flush()
            db.add(
                ChatAttachment(
                    attachment_id=uuid4(),
                    conversation_id=conversation_id,
                    surface=ChatSurface.MATRIX,
                    address=room_id,
                    attached_at=now,
                    detached_at=None,
                )
            )
            return BoundRoom(room_id=room_id, conversation_id=conversation_id)

    async def attachment_of_room(self, room_id: str) -> tuple[UUID, UUID] | None:
        """The conversation *room_id* holds a copy of and the attachment holding it, or None.

        Both, because a subscriber needs one of each: the conversation to read the log, and the
        attachment to key the position it reads from. Addressed by room rather than answered from
        the binding, because a subscriber is told which room it is reading for and a room that is
        not the bound one holds nothing.
        """
        async with self._sessions() as db:
            found = (
                await db.execute(
                    select(ChatAttachment.conversation_id, ChatAttachment.attachment_id).where(
                        ChatAttachment.surface == ChatSurface.MATRIX,
                        ChatAttachment.address == room_id,
                        ChatAttachment.detached_at.is_(None),
                    )
                )
            ).first()
            return None if found is None else (found.conversation_id, found.attachment_id)


@dataclass(frozen=True)
class ConversationFacts:
    """Console-authored events belonging to one conversation, for the caller to append.

    **Bodies rather than rows.** An authored event's position is allocated under the conversation's
    lock, so only the transaction that writes it can say where it goes — and that is the transaction
    that acknowledges the batch (`sync.MatrixSyncStore.advance`), which is what keeps the record of
    what a pass decided from being lost by a crash after the watermark moved.

    `session_id` is absent where no session was up to refuse the batch: what a refusal is about is
    the conversation, which exists as soon as the room is bound.
    """

    conversation_id: UUID
    session_id: UUID | None
    bodies: tuple[session_events.AuthoredBody, ...]

    def then(self, *more: session_events.AuthoredBody) -> ConversationFacts:
        return ConversationFacts(
            conversation_id=self.conversation_id, session_id=self.session_id, bodies=self.bodies + more
        )


@dataclass(frozen=True)
class PromptAccepted:
    """The batch is a prompt item on the live session, and a turn will answer it."""

    item_id: UUID


@dataclass(frozen=True)
class PromptRejected:
    """The batch was refused, and is not coming back: what to say, and the fact that records it.

    `facts` is None only where no room is bound, so there is no conversation to record against —
    and nowhere to say it either, which makes the two absences the same one.
    """

    reason: PromptRejection
    facts: ConversationFacts | None


type Admission = PromptAccepted | PromptRejected


class MatrixTurns:
    """Ingress: offers the operator's messages to the conversation the room is attached to.

    Refusal is a first-class answer and a terminal one. Ingress offers the prompt to the durable
    conversation without resolving, creating or replacing a session. Neutral runtime supervision
    creates one when needed; admission still refuses an in-flight turn or an already queued prompt.
    What the caller does with a rejection is acknowledge it, recording the row this hands back in
    the same transaction (`sync.MatrixSyncStore.advance`).

    A prompt this accepts is the conversation's, not the accepting session's, so a session that dies
    before claiming it strands nothing: its replacement finds the same queued row. What the record
    keeps against the events themselves (`ingress_ledger`) is only what makes a re-delivery
    recognisable.
    """

    def __init__(
        self,
        config: MatrixConfig,
        conversations: MatrixConversationStore,
        chat_store: SessionStore,
        identities: PostgresOperatorIdentityStore,
        ledger: IngressLedger,
    ):
        self._config = config
        self._conversations = conversations
        self._chat_store = chat_store
        self._identities = identities
        self._ledger = ledger

    async def offer(self, messages: Sequence[InboundMessage]) -> Admission:
        """Enqueue `messages` as one prompt, or say why the session would not take them.

        The whole batch or none of it: a partial enqueue would leave half a sentence delivered and
        half of it rejected, which is a worse answer than either.
        """
        return await self._enqueue(_as_prompt(messages), tuple(message.event_id for message in messages))

    async def _enqueue(self, prompt_text: str, event_ids: tuple[str, ...]) -> Admission:
        # The binding is read for the room alone — which session is serving comes through the
        # conversation — because the room is the address this prompt's origin names.
        binding = await self._conversations.bound_room()
        if binding is None:
            logger.info("Matrix: no room bound, rejecting %d event(s)", len(event_ids))
            return PromptRejected(reason=PromptRejection.NO_SESSION, facts=None)
        operator_id = await self._identities.resolve_configured_external_user_key(self._config.operator_subject)
        try:
            item_id = await self._chat_store.enqueue_conversation_prompt(
                operator_id,
                binding.conversation_id,
                prompt_text,
                _origin(binding.room_id, event_ids),
                self._ledger.carrying(event_ids),
            )
        except KeyError:
            logger.info("Matrix: bound conversation %s is gone, rejecting the batch", binding.conversation_id)
            return self._refused(binding, None, PromptRejection.NO_SESSION, prompt_text)
        except PromptRefusedError as refusal:
            # Admission is `enqueue_prompt`'s alone, decided under `SELECT … FOR UPDATE`: a status
            # read here could only agree with a decision that had not been made yet.
            logger.info("Matrix: conversation %s rejected the batch: %s", binding.conversation_id, refusal.reason)
            return self._refused(binding, None, refusal.reason, prompt_text)
        return PromptAccepted(item_id=item_id)

    def _refused(
        self, binding: BoundRoom, session_id: UUID | None, reason: PromptRejection, prompt_text: str
    ) -> PromptRejected:
        return PromptRejected(
            reason=reason,
            facts=ConversationFacts(
                conversation_id=binding.conversation_id,
                session_id=session_id,
                bodies=(PromptRejectedBody(reason=reason, text=prompt_text),),
            ),
        )

    async def unreadable(self, events: Sequence[UnmappableEvent]) -> ConversationFacts | None:
        """The facts for events Haku has no way to read, one each, for the caller to append.

        None where no room is bound, on the same terms as `PromptRejected.facts`: there is no
        conversation to record against, and no room to say it in either.
        """
        binding = await self._conversations.bound_room()
        if binding is None:
            return None
        return ConversationFacts(
            conversation_id=binding.conversation_id,
            session_id=None,
            bodies=tuple(UnreadableInputBody(media_type=event.msgtype) for event in events),
        )


def _origin(room_id: str, event_ids: tuple[str, ...]) -> MatrixOrigin:
    """This batch, as the origin the rest of the console may hold but not read.

    One origin rather than one per message: a batch arrives through a single attachment and
    becomes a single prompt, so the room is the origin and the events are what it folded.

    **The room travels with the events**, even though only one room is serviced today: a surface
    deciding whether a prompt is already in front of its reader compares origins, and a bare event
    id cannot tell a sibling room's copy from this room's the moment one bot serves several.
    """
    return MatrixOrigin(address=room_id, refs=event_ids)


def _as_prompt(messages: Sequence[InboundMessage]) -> str:
    """Render a batch as one prompt: what the operator said, in the order they said it.

    The event ids are not rendered into it: they ride on the prompt item's origin, which is what
    the room read tools resolve a citation through and what a reply answering a specific message
    addresses itself with.
    """
    return "\n".join(message.body for message in messages)

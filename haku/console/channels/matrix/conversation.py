"""The conversations the rooms Haku services are attached to.

A `channel_attachment` row binds a room to a conversation, and the conversation outlives every
session that runs under it. One bot serves many rooms — every room the operator invites Haku into
is its own conversation under its own attachment. The binding and ingress are Matrix's;
conversation history and turn execution are channel-neutral, and a replacement joins the
conversation the attachment already names rather than re-pointing the attachment.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.channels.matrix.client import InboundMessage, UnmappableEvent
from haku.console.channels.matrix.config import Config
from haku.console.channels.matrix.ingress_ledger import IngressLedger
from haku.console.chat_models import ChannelSurface, MatrixOrigin, PromptRejection, RuntimeKind
from haku.console.conversation import conversation_event
from haku.console.database_schema import ChannelAttachmentRow, Conversation, Operator
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.session.launch_identity import LaunchAuthorizer, LaunchIdentity
from haku.console.session.store import PromptRefusedError, Store

logger = logging.getLogger(__name__)

# There is no row to lock before a room's first bind. This transaction-scoped mutex keeps two
# concurrent binds from opening different conversations for one room before the live-attachment
# uniqueness constraint can choose a winner.
_BIND_ADVISORY_LOCK = 0x4D58_4244  # "MXBD"


async def live_attachment(db: AsyncSession, room_id: str) -> UUID | None:
    """This room's live attachment, which is what its deliveries hang off.

    None where the room holds no conversation — a room this console never bound, or one detached
    since — in which case there is nothing to record a send against and the room notice is the only
    account of it.

    Takes the caller's session so a caller can read it inside its own transaction.
    """
    attachment_id: UUID | None = await db.scalar(
        select(ChannelAttachmentRow.attachment_id).where(
            ChannelAttachmentRow.surface == ChannelSurface.MATRIX,
            ChannelAttachmentRow.address == room_id,
            ChannelAttachmentRow.detached_at.is_(None),
        )
    )
    return attachment_id


@dataclass(frozen=True)
class RoomAttachment:
    """One live binding: a room, the conversation it holds a copy of, and the attachment holding it.

    All three, because the channel's state hangs off each: the room is the address events are sent
    to, the conversation is what the room's subscriber reads, and the attachment keys the cursor,
    outbox, revisions and send budget that delivery runs on.
    """

    room_id: str
    conversation_id: UUID
    attachment_id: UUID


async def _live_room_binding(db: AsyncSession, room_id: str) -> RoomAttachment | None:
    row = (
        await db.execute(
            select(
                ChannelAttachmentRow.address, ChannelAttachmentRow.conversation_id, ChannelAttachmentRow.attachment_id
            ).where(
                ChannelAttachmentRow.surface == ChannelSurface.MATRIX,
                ChannelAttachmentRow.address == room_id,
                ChannelAttachmentRow.detached_at.is_(None),
            )
        )
    ).first()
    return (
        None
        if row is None
        else RoomAttachment(room_id=row.address, conversation_id=row.conversation_id, attachment_id=row.attachment_id)
    )


class ConversationStore:
    """Which durable conversation each bound room holds a copy of.

    Runtime sessions are deliberately absent from the binding. Neutral supervision creates and
    replaces them under the conversation, so the channel has no pointer to tend or re-aim.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        launch_authorizer: LaunchAuthorizer | None = None,
        default_agent_id: UUID | None = None,
        default_runtime_kind: RuntimeKind = RuntimeKind.CLAUDE_CODE,
    ):
        self._sessions = sessions
        self._launch_authorizer = launch_authorizer
        self._default_agent_id = default_agent_id
        self._default_runtime_kind = default_runtime_kind

    def configure_launch_identity(
        self,
        authorizer: LaunchAuthorizer,
        *,
        default_agent_id: UUID,
        default_runtime_kind: RuntimeKind = RuntimeKind.CLAUDE_CODE,
    ) -> None:
        """Configure production first-bind identity selection after app composition."""
        self._launch_authorizer = authorizer
        self._default_agent_id = default_agent_id
        self._default_runtime_kind = default_runtime_kind

    async def attachment(self, room_id: str) -> UUID | None:
        async with self._sessions() as db:
            return await live_attachment(db, room_id)

    async def live_attachments(self) -> tuple[RoomAttachment, ...]:
        """Every room this console services, oldest binding first."""
        async with self._sessions() as db:
            rows = (
                await db.execute(
                    select(
                        ChannelAttachmentRow.address,
                        ChannelAttachmentRow.conversation_id,
                        ChannelAttachmentRow.attachment_id,
                    )
                    .where(
                        ChannelAttachmentRow.surface == ChannelSurface.MATRIX,
                        ChannelAttachmentRow.detached_at.is_(None),
                    )
                    .order_by(ChannelAttachmentRow.attached_at, ChannelAttachmentRow.attachment_id)
                )
            ).all()
            return tuple(
                RoomAttachment(
                    room_id=row.address, conversation_id=row.conversation_id, attachment_id=row.attachment_id
                )
                for row in rows
            )

    async def bind_room(self, room_id: str, operator_id: UUID) -> RoomAttachment:
        """Attach `room_id`, opening the conversation it holds a copy of; idempotent per room.

        A room already live keeps its binding; a new room binds beside the existing ones, because
        one bot serves many rooms and each room is its own conversation. Binding opens the
        conversation in the same transaction — so a bound room always has one, and the attachment
        outlives every session that serves it: a replacement joins the conversation the attachment
        already names instead of the attachment being re-pointed at it.

        Read-then-insert rather than insert-or-nothing, serialized by the sync loop's election:
        only its leader binds rooms, so the read and the insert cannot interleave with another
        replica's. `uq_channel_attachment_live_address` is the backstop if that ever stops holding.
        """
        async with self._sessions() as db, db.begin():
            # There is no row to lock before a room's first bind, so serialize the empty-check with
            # a transaction advisory lock. The Operator row is also locked by the authorizer below;
            # both locks remain held through the conversation and attachment inserts.
            await db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _BIND_ADVISORY_LOCK})
            await db.get(Operator, operator_id, with_for_update=True)
            if (live := await _live_room_binding(db, room_id)) is not None:
                return live
            now = datetime.datetime.now(datetime.UTC)
            identity: LaunchIdentity | None = None
            if self._launch_authorizer is not None:
                if self._default_agent_id is None:
                    raise RuntimeError("Matrix launch identity is not configured")
                identity = await self._launch_authorizer(
                    db, operator_id, self._default_agent_id, self._default_runtime_kind
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
            # mappers leaves their inserts in mapper-name order — `channel_attachment` ahead of
            # `conversation`, which the constraint rejects.
            await db.flush()
            attachment_id = uuid4()
            db.add(
                ChannelAttachmentRow(
                    attachment_id=attachment_id,
                    conversation_id=conversation_id,
                    surface=ChannelSurface.MATRIX,
                    address=room_id,
                    attached_at=now,
                    detached_at=None,
                )
            )
            return RoomAttachment(room_id=room_id, conversation_id=conversation_id, attachment_id=attachment_id)


@dataclass(frozen=True)
class ConversationFacts:
    """Console-authored events belonging to one conversation, for the caller to append.

    **Bodies rather than rows.** An authored event's position is allocated under the conversation's
    lock, so only the transaction that writes it can say where it goes — and that is the transaction
    that acknowledges the batch (`sync.SyncStore.advance`), which is what keeps the record of
    what a pass decided from being lost by a crash after the watermark moved.

    `session_id` is absent where no session was up to refuse the batch: what a refusal is about is
    the conversation, which exists as soon as the room is bound.
    """

    conversation_id: UUID
    session_id: UUID | None
    bodies: tuple[conversation_event.AuthoredEvent, ...]

    def then(self, *more: conversation_event.AuthoredEvent) -> ConversationFacts:
        return ConversationFacts(
            conversation_id=self.conversation_id, session_id=self.session_id, bodies=self.bodies + more
        )


@dataclass(frozen=True)
class PromptAccepted:
    """The batch is a durable inbox prompt on the conversation; a runner will admit it (#4667)."""

    prompt_id: UUID


@dataclass(frozen=True)
class PromptRejected:
    """The batch was refused, and is not coming back: what to say, and the fact that records it.

    `facts` is always there to append: a batch is only ever offered for a room with a live
    binding, and the conversation a refusal is about exists from the moment the room is bound.
    """

    reason: PromptRejection
    facts: ConversationFacts


type Admission = PromptAccepted | PromptRejected


class Turns:
    """Ingress: offers the operator's messages to the conversation their room is attached to.

    Refusal is a first-class answer and a terminal one. Ingress offers the prompt to the durable
    conversation without resolving, creating or replacing a session. Neutral runtime supervision
    creates one when needed; admission still refuses an in-flight turn or an already queued prompt.
    What the caller does with a rejection is acknowledge it, recording the row this hands back in
    the same transaction (`sync.SyncStore.advance`).

    The binding is the caller's argument: the sync pass dispatches a batch by room, so which
    conversation a batch is offered to was resolved where the room was.

    A prompt this accepts is the conversation's, not the accepting session's, so a session that dies
    before claiming it strands nothing: its replacement finds the same queued row. What the record
    keeps against the events themselves (`ingress_ledger`) is only what makes a re-delivery
    recognisable.
    """

    def __init__(
        self, config: Config, session_store: Store, identities: PostgresOperatorIdentityStore, ledger: IngressLedger
    ):
        self._config = config
        self._session_store = session_store
        self._identities = identities
        self._ledger = ledger

    async def offer(self, binding: RoomAttachment, messages: Sequence[InboundMessage]) -> Admission:
        """Enqueue `messages` as one prompt, or say why the session would not take them.

        The whole batch or none of it: a partial enqueue would leave half a sentence delivered and
        half of it rejected, which is a worse answer than either.
        """
        return await self._enqueue(binding, _as_prompt(messages), tuple(message.event_id for message in messages))

    async def _enqueue(self, binding: RoomAttachment, prompt_text: str, event_ids: tuple[str, ...]) -> Admission:
        operator_id = await self._identities.resolve_configured_external_user_key(self._config.operator_subject)
        try:
            # The refusing variant, deliberately: this channel promises a mid-turn batch is
            # rejected, not held (<SPEC.md> § Batching and admission), and under the inbox that
            # policy is the surface's to choose (<../../x/prompt_inbox.py>).
            prompt_id = await self._session_store.submit_exclusive_prompt(
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
            # Admission is `submit_exclusive_prompt`'s alone, decided under `SELECT … FOR UPDATE`:
            # a status read here could only agree with a decision that had not been made yet.
            logger.info("Matrix: conversation %s rejected the batch: %s", binding.conversation_id, refusal.reason)
            return self._refused(binding, None, refusal.reason, prompt_text)
        return PromptAccepted(prompt_id=prompt_id)

    def _refused(
        self, binding: RoomAttachment, session_id: UUID | None, reason: PromptRejection, prompt_text: str
    ) -> PromptRejected:
        return PromptRejected(
            reason=reason,
            facts=ConversationFacts(
                conversation_id=binding.conversation_id,
                session_id=session_id,
                bodies=(conversation_event.PromptRejected(reason=reason, text=prompt_text),),
            ),
        )

    async def unreadable(self, binding: RoomAttachment, events: Sequence[UnmappableEvent]) -> ConversationFacts:
        """The facts for events Haku has no way to read, one each, for the caller to append."""
        return ConversationFacts(
            conversation_id=binding.conversation_id,
            session_id=None,
            bodies=tuple(conversation_event.UnreadableInput(media_type=event.msgtype) for event in events),
        )


def _origin(room_id: str, event_ids: tuple[str, ...]) -> MatrixOrigin:
    """This batch, as the origin the rest of the console may hold but not read.

    One origin rather than one per message: a batch arrives through a single attachment and
    becomes a single prompt, so the room is the origin and the events are what it folded.

    **The room travels with the events**: a surface deciding whether a prompt is already in front
    of its reader compares origins, and a bare event id cannot tell a sibling room's copy from this
    room's while one bot serves several.
    """
    return MatrixOrigin(address=room_id, refs=event_ids)


def _as_prompt(messages: Sequence[InboundMessage]) -> str:
    """Render a batch as one prompt: what the operator said, in the order they said it.

    The event ids are not rendered into it: they ride on the prompt item's origin, which is what
    the room read tools resolve a citation through and what a reply answering a specific message
    addresses itself with.
    """
    return "\n".join(message.body for message in messages)

"""One owner per live attachment, and the set of them the sync leader runs.

An **attachment reconciler** coordinates everything the console owes one room: the conversation
cursor its subscriber reads from, the reply outbox its drain says, the span revisions both edit
through, and the send budget they spend. All of that state is attachment-addressed in the
database; this is the one process-side owner driving it, so starting a second room is starting a
second reconciler rather than fighting a singleton.

The reconcilers run on the sync leader — the one Matrix `/sync` owner for the user-wide token —
which is what makes each of them singular cluster-wide without an election of its own: the sweep
(`AttachmentReconcilers.sweep`) runs under the `MXSY` lock, starts a reconciler for every live
attachment, and stops the one an attachment losing its binding leaves behind. A leadership change
moves all of them together, costing the takeover latency and nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID

from haku.console.x.channels.matrix.conversation import RoomAttachment
from haku.console.x.channels.matrix.conversation_subscriber import ConversationSubscriber
from haku.console.x.channels.matrix.outbox import RoomOutboxDrain

logger = logging.getLogger(__name__)


class AttachmentReconciler:
    """Everything one live attachment's room is owed, under one owner.

    Holds the attachment's subscriber (cursor, sealed notices, span lines, outbox enqueue) and its
    outbox drain (replies), which share the attachment's own pacer. Stopping the reconciler stops
    everything that can decide to send into its room.
    """

    def __init__(self, binding: RoomAttachment, subscriber: ConversationSubscriber, drain: RoomOutboxDrain) -> None:
        self.binding = binding
        self._subscriber = subscriber
        self._drain = drain

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with self._subscriber.run(), self._drain.run():
            yield


# Builds the reconciler for one binding; the sync service is the factory, because the subscriber
# and drain need the credential-holding frontend and the binding's pacer only it can hand out.
ReconcilerFactory = Callable[[RoomAttachment], Awaitable[AttachmentReconciler]]


class AttachmentReconcilers:
    """The running reconcilers, one per live attachment.

    Driven by the sync leader: `sweep` per pass against the live bindings, `aclose` when
    leadership ends. Bindings change only through the leader's own passes (an invite, an
    adoption) or a database edit, both of which the next pass's sweep observes.
    """

    def __init__(self, factory: ReconcilerFactory) -> None:
        self._factory = factory
        self._running: dict[UUID, AsyncExitStack] = {}

    async def sweep(self, live: Sequence[RoomAttachment]) -> None:
        """Start a reconciler for every live binding and stop the ones nothing holds any more."""
        wanted = {binding.attachment_id for binding in live}
        for attachment_id in [attachment_id for attachment_id in self._running if attachment_id not in wanted]:
            logger.info("Matrix: attachment %s is no longer live; stopping its reconciler", attachment_id)
            await self._stop(attachment_id)
        for binding in live:
            if binding.attachment_id in self._running:
                continue
            reconciler = await self._factory(binding)
            stack = AsyncExitStack()
            await stack.enter_async_context(reconciler.run())
            self._running[binding.attachment_id] = stack
            logger.info("Matrix: reconciling %s for attachment %s", binding.room_id, binding.attachment_id)

    async def _stop(self, attachment_id: UUID) -> None:
        await self._running.pop(attachment_id).aclose()

    async def aclose(self) -> None:
        for attachment_id in list(self._running):
            await self._stop(attachment_id)

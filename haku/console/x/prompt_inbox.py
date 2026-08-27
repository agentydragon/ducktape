"""The durable prompt inbox: what the Console has accepted and still owes a runner (#4667 stage 3).

A submitted prompt is a command before it is conversation. The row holds the authoritative text
and origin from acceptance until the runner's `prompt.admitted` names it, at which point the
journal consumer materialises the transcript item from the row and stamps the admission —
`journal_consumer.py` owns that transition. What lives here is the Console's own half of the state
machine: pending → withdrawn, and the queries admission validates against.

Nothing here decides *whether* to accept a prompt. Admission policy — busy sessions, closed
conversations, who may speak — stays with the surface that hears the operator; this module records
what it accepted.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import PromptOrigin
from haku.console.database_schema import SubmittedPrompt


class PromptNotPendingError(LookupError):
    """The prompt is not in the pending state the operation requires — unknown, already admitted,
    or already withdrawn. The message says which."""


async def submit(
    db: AsyncSession,
    *,
    conversation_id: UUID,
    text: str,
    origin: PromptOrigin,
    now: datetime,
    prompt_id: UUID | None = None,
) -> SubmittedPrompt:
    """Accept a prompt into the conversation's inbox, inside the caller's transaction.

    *prompt_id* is minted here unless the caller already promised one to its channel; either way it
    is the id the runner will echo back verbatim.
    """
    row = SubmittedPrompt(
        prompt_id=prompt_id if prompt_id is not None else uuid4(),
        conversation_id=conversation_id,
        text=text,
        origin=origin,
        submitted_at=now,
    )
    db.add(row)
    await db.flush()
    return row


async def withdraw(db: AsyncSession, prompt_id: UUID, *, now: datetime) -> SubmittedPrompt:
    """Take a pending prompt back, inside the caller's transaction.

    Locked, because withdrawal races admission: whichever transaction stamps the row first wins,
    and the loser is told the state it lost to.
    """
    row = await db.get(SubmittedPrompt, prompt_id, with_for_update=True)
    if row is None:
        raise PromptNotPendingError(f"no submitted prompt under this id: {prompt_id=}")
    if row.withdrawn_at is not None:
        raise PromptNotPendingError(f"the prompt is already withdrawn: {prompt_id=}")
    if row.admitted_at is not None:
        raise PromptNotPendingError(f"the runner has already admitted the prompt: {prompt_id=}")
    row.withdrawn_at = now
    return row

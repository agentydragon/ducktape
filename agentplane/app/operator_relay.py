"""A byte stream from an upstream service, relayed to an operator's browser for as long as their
operator session lasts."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import Request

from agentplane.app.database_updates import Channel
from agentplane.app.operator_sessions import OperatorSessionStore, operator_session_row
from agentplane.app.shutdown import Drain, until_done
from agentplane.postgres.wakeups import WakeupListener

logger = logging.getLogger(__name__)


def operator_relay(
    request: Request,
    shutdown: Drain,
    updates: WakeupListener,
    sessions: OperatorSessionStore,
    chunks: AsyncIterator[bytes],
    label: str,
    errors: tuple[type[Exception], ...],
) -> AsyncIterator[bytes]:
    """`chunks` until the operator session ends -- a logout on any replica, or its expiry, which
    they share through PostgreSQL -- the app drains, or the browser goes. Headers are already sent
    when `errors` interrupt the upstream, so the connection just ends and the browser reconnects."""
    session_id = operator_session_row(request).id

    async def session_over() -> None:
        await sessions.until_ended(session_id, updates.changes[Channel.OPERATOR_SESSIONS])

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in shutdown.until(until_done(chunks, session_over)):
                if await request.is_disconnected():
                    return
                yield chunk
        except errors:
            logger.warning("%s stream interrupted", label, exc_info=True)

    return body()

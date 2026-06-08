"""Chat middleware that injects pg_notify notifications into the grader's context.

Drains `GraderState.notification_queue` before each model call. If notifications are
pending, appends a single user message summarizing them so the model sees newly-arrived
work — covering both wake-from-sleep and mid-work arrivals through one code path. (Port of
the agent_core `GraderNotificationsHandler` that returned `InjectItems` on `on_before_sample`.)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from agent_framework import ChatContext, Message, chat_middleware

# Precise callable type for a chat middleware (a member of MAF's MiddlewareTypes union).
ChatMiddlewareCallable = Callable[[ChatContext, Callable[[], Awaitable[None]]], Awaitable[None]]

if TYPE_CHECKING:
    from props.db.notifications import GradingPendingNotification

logger = logging.getLogger(__name__)


def _format_notifications(notifications: list[GradingPendingNotification]) -> str:
    lines = [
        f"{len(notifications)} new grading notification(s):",
        *[f"  - {n.operation} on {n.item.table}" for n in notifications],
        "Check pending work and continue grading.",
    ]
    return "\n".join(lines)


def notification_chat_middleware(queue: list[GradingPendingNotification]) -> ChatMiddlewareCallable:
    """Chat middleware that drains `queue` and injects a summary user message per model call."""

    @chat_middleware
    async def middleware(context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        if queue:
            notifications = list(queue)
            queue.clear()
            logger.info("Delivering %d grading notification(s)", len(notifications))
            context.messages.append(Message("user", [_format_notifications(notifications)]))
        await call_next()

    return middleware

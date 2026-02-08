"""Handler that injects pg_notify notifications into the grader's context.

Drains DaemonState.notification_queue on each on_before_sample() call.
If notifications are pending, returns InjectItems with a UserMessage
summarizing them. This covers both wake-from-sleep and mid-work arrivals
through a single code path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_core.handler import BaseHandler
from agent_core.loop_control import InjectItems, LoopDecision, NoAction
from openai_utils.model import UserMessage

if TYPE_CHECKING:
    from props.agents.grader.notifications import GradingPendingNotification

logger = logging.getLogger(__name__)


def _format_notifications(notifications: list[GradingPendingNotification]) -> UserMessage:
    """Format drained notifications as a single UserMessage."""
    lines = [f"{len(notifications)} new grading notification(s):"]
    for n in notifications:
        lines.append(f"  - {n.operation} on {n.item.table}")
    lines.append("Check list_pending for updated work.")
    return UserMessage.text("\n".join(lines))


class GraderNotificationsHandler(BaseHandler):
    """Deliver pg_notify notifications as a batched UserMessage via InjectItems.

    Polls DaemonState.notification_queue. If non-empty, drains it and
    returns InjectItems with a summary message. Otherwise returns NoAction.
    """

    def __init__(self, queue: list[GradingPendingNotification]) -> None:
        self._queue = queue

    def on_before_sample(self) -> LoopDecision:
        if not self._queue:
            return NoAction()

        notifications = list(self._queue)
        self._queue.clear()

        logger.info("Delivering %d grading notification(s)", len(notifications))
        return InjectItems(items=[_format_notifications(notifications)])

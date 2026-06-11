"""NotificationsHandler for delivering MCP notifications to the agent."""

from __future__ import annotations

import logging
from collections.abc import Callable

from agent_core.handler import BaseHandler
from agent_core.loop_control import InjectItems, NoAction
from mcp_infra.notifications.types import NotificationsBatch, ResourcesServerNotice
from openai_utils.model import UserMessage

logger = logging.getLogger(__name__)


def format_notifications_message(batch: NotificationsBatch) -> UserMessage:
    """Format MCP notifications as a user message.

    Caller must ensure batch has at least one notification.
    """
    # Filter to only include servers with actual updates or list changes
    resources_filtered: dict[str, ResourcesServerNotice] = {
        name: entry for name, entry in batch.resources.items() if entry.updated or entry.list_changed
    }

    payload = NotificationsBatch(resources=resources_filtered).model_dump_json(exclude_defaults=True, exclude_none=True)

    # Insert as input-side user message, clearly tagged as a system notification
    return UserMessage.text(f"<system notification>\n{payload}\n</system notification>")


class NotificationsHandler(BaseHandler):
    """Deliver MCP notifications as one batched system message via InjectItems.

    Polls a provided notifications buffer for buffered updates and, if present, returns an
    InjectItems() decision with a single UserMessage that encodes the per-server resource
    version changes.
    """

    def __init__(self, poll: Callable[[], NotificationsBatch]) -> None:
        self._poll = poll
        self._msg_counter = 0

    def on_before_sample(self):
        batch = self._poll()
        notification_count = batch.count_notifications()

        if notification_count == 0:
            logger.debug("NotificationsHandler: no updates")
            return NoAction()

        self._msg_counter += 1
        logger.info(
            "NotificationsHandler: delivering %d notifications (msg #%d)", notification_count, self._msg_counter
        )
        return InjectItems(items=(format_notifications_message(batch),))

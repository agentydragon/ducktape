from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from agent_core.handler import BaseHandler
from agent_core.loop_control import Abort, InjectItems, NoAction
from mcp_infra.notifications.types import NotificationsBatch

# Import just the NotificationsBatch type - it's a lightweight Pydantic model
from x.agent_server.notifications.handler import format_notifications_message
from x.agent_server.server.bus import ServerBus


class ServerModeHandler(BaseModel, BaseHandler):
    """UI mode handler combining notifications delivery and loop control.

    Single handler that:
    1. Checks for end_turn signal and aborts if set
    2. Delivers any pending MCP notifications as system messages

    Note: Tool policy (typically RequireAnyTool) is configured at agent construction time.
    """

    bus: ServerBus
    poll_notifications: Callable[[], NotificationsBatch]  # Light dependency - just a function

    def on_before_sample(self):
        # First check end_turn
        if self.bus.consume_end_turn():
            return Abort()

        # Check for and deliver notifications using shared logic
        batch = self.poll_notifications()

        if batch.has_notifications():
            # Inject notification message as user input
            return InjectItems(items=(format_notifications_message(batch),))

        # No notifications - just continue
        return NoAction()

    # No per-tool interception needed; approvals are enforced by Policy Gateway middleware

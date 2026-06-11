from __future__ import annotations

from collections.abc import Callable

from agent_core.handler import BaseHandler
from mcp_infra.compositor.compositor import Compositor
from mcp_infra.display.event_renderer import DisplayEventsHandler
from mcp_infra.notifications.types import NotificationsBatch
from x.agent_server.agent_types import AgentID
from x.agent_server.notifications.handler import NotificationsHandler
from x.agent_server.persist.handler import RunPersistenceHandler
from x.agent_server.persist.types import Persistence
from x.agent_server.server.bus import ServerBus
from x.agent_server.server.mode_handler import ServerModeHandler
from x.agent_server.server.runtime import UiEventHandler


def build_handlers(
    *,
    poll_notifications: Callable[[], NotificationsBatch],
    manager: UiEventHandler,
    persistence: Persistence,
    agent_id: AgentID,
    compositor: Compositor | None = None,
    ui_bus: ServerBus | None = None,
) -> tuple[list[BaseHandler], RunPersistenceHandler]:
    persist_handler = RunPersistenceHandler(persistence=persistence, agent_id=agent_id)
    handlers: list[BaseHandler] = [manager, persist_handler]
    if ui_bus is not None:
        handlers.extend(
            [
                ServerModeHandler(bus=ui_bus, poll_notifications=poll_notifications),
                DisplayEventsHandler(compositor=compositor),
            ]
        )
    else:
        # Production/non-UI path: flush MCP notifications via NotificationsHandler
        handlers.append(NotificationsHandler(poll_notifications))
    return handlers, persist_handler

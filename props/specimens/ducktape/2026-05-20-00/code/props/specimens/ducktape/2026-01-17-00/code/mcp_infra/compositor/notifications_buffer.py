from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from mcp import types as mcp_types

from mcp_infra.notifications.types import NotificationsBatch, ResourcesServerNotice
from mcp_infra.resource_utils import derive_origin_server

if TYPE_CHECKING:
    from mcp_infra.compositor.server import Compositor

logger = logging.getLogger(__name__)


class _ServerNoticeAccumulator:
    """Mutable accumulator for per-server notifications."""

    def __init__(self) -> None:
        self.updated: set[str] = set()
        self.list_changed: bool = False

    def to_frozen(self) -> ResourcesServerNotice:
        """Convert to immutable ResourcesServerNotice."""
        return ResourcesServerNotice(updated=frozenset(self.updated), list_changed=self.list_changed)


class _ResourceNotificationHandler(MessageHandler):
    """Message handler that forwards resource notifications to NotificationsBuffer."""

    def __init__(self, owner: NotificationsBuffer) -> None:
        self._buffer = owner

    # Override with narrower types than base MessageHandler (which accepts Any)
    async def on_resource_updated(self, message: mcp_types.ResourceUpdatedNotification) -> None:
        await self._buffer._on_updated(message)

    async def on_resource_list_changed(self, message: mcp_types.ResourceListChangedNotification) -> None:
        await self._buffer._on_list_changed(message)


class NotificationsBuffer:
    """Capture MCP resource notifications and expose a simple poll/peek API.

    - Attaches to a FastMCP Client via `message_handler` when the client is created.
    - Groups updates by server with deduplicated URIs using frozenset.
    - Server names are derived via Compositor mount prefixes when possible; otherwise set to 'unknown'.
    - Hooks can be registered to react to updates (e.g., push UI snapshots).
    """

    def __init__(self, *, client: Client | None = None, compositor: Compositor) -> None:
        self._client = client
        self._compositor = compositor
        # Per-server accumulator (mutable during accumulation, converted to NotificationsBatch on poll/peek)
        self._servers: dict[str, _ServerNoticeAccumulator] = {}
        self.handler: MessageHandler = _ResourceNotificationHandler(self)
        # Subscribe to compositor-level notifications when available so we don't
        # rely solely on client message forwarding (which may be disabled for
        # in-proc mounts).
        self._compositor.add_resource_updated_listener(self._on_resource_listener)
        self._compositor.add_resource_list_change_listener(self._on_list_listener)
        # Capture any pending resource list changes emitted before the buffer attached
        for server in self._compositor.pop_recent_resource_list_changes():
            self._servers.setdefault(server, _ServerNoticeAccumulator()).list_changed = True

    def peek(self) -> NotificationsBatch:
        """Peek at buffered notifications without clearing them."""
        resources = {server: acc.to_frozen() for server, acc in self._servers.items()}
        return NotificationsBatch(resources=resources)

    def poll(self) -> NotificationsBatch:
        """Poll and clear buffered notifications, returning grouped batch."""
        batch = self.peek()
        self._servers.clear()
        return batch

    async def _on_updated(self, message: mcp_types.ResourceUpdatedNotification) -> None:
        # Add URI to the server's update set
        uri_str = str(message.params.uri)
        server = await self._derive_server(uri_str)
        self._servers.setdefault(server, _ServerNoticeAccumulator()).updated.add(uri_str)

    async def _on_list_changed(self, message: mcp_types.ResourceListChangedNotification) -> None:
        # Attribute origin using compositor-captured child notifications when available
        names = list(self._compositor.pop_recent_resource_list_changes())
        for name in names:
            self._servers.setdefault(name, _ServerNoticeAccumulator()).list_changed = True

    async def _derive_server(self, uri: str) -> str:
        """Derive origin server from resource URI using all compositor mounts."""
        mount_names = await self._compositor._mount_names()
        return derive_origin_server(uri, mount_names)

    async def _on_resource_listener(self, name: str, uri: str) -> None:
        self._servers.setdefault(name, _ServerNoticeAccumulator()).updated.add(uri)

    async def _on_list_listener(self, name: str) -> None:
        self._servers.setdefault(name, _ServerNoticeAccumulator()).list_changed = True

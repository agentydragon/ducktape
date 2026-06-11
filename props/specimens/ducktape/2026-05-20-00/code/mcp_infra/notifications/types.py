from __future__ import annotations

from pydantic import BaseModel, Field


class ResourcesServerNotice(BaseModel):
    """Per-server resources notice.

    - updated: immutable set of resource URIs updated for this server
    - list_changed: whether resources/list changed for this server
    """

    updated: frozenset[str] = Field(default_factory=frozenset, description="Resource URIs that were updated")
    list_changed: bool = Field(default=False, description="Whether resources/list changed")


class NotificationsBatch(BaseModel):
    """Buffered notifications grouped by server for efficient consumption."""

    resources: dict[str, ResourcesServerNotice] = Field(
        default_factory=dict, description="Per-server resources notice: {server -> {updated, list_changed}}"
    )

    @property
    def resource_list_changed(self) -> set[str]:
        """Get set of server names where resources/list changed."""
        return {server for server, notice in self.resources.items() if notice.list_changed}

    def count_notifications(self) -> int:
        """Count total notifications (updated URIs + list changes)."""
        return sum(len(notice.updated) for notice in self.resources.values()) + sum(
            1 for notice in self.resources.values() if notice.list_changed
        )

    def has_notifications(self) -> bool:
        """Check if batch has any notifications worth delivering."""
        return any(notice.updated or notice.list_changed for notice in self.resources.values())

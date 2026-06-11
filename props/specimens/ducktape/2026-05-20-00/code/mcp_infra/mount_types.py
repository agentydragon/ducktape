"""Mount event types shared between compositor and resources."""

from enum import StrEnum


class MountEvent(StrEnum):
    """Events emitted when mounts change state."""

    MOUNTED = "mounted"
    UNMOUNTED = "unmounted"
    STATE = "state"

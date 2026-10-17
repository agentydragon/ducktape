"""Mount event types emitted by the agent CLI compositor."""

from enum import StrEnum


class MountEvent(StrEnum):
    """Events emitted when mounts change state."""

    MOUNTED = "mounted"
    UNMOUNTED = "unmounted"
    STATE = "state"

"""The Session Dispatcher's NOTIFY channel: an invalidation, never the durable state."""

from enum import StrEnum

from sqlalchemy.engine import URL

from agentplane.postgres.wakeups import WakeupListener


class Channel(StrEnum):
    # A Dispatch was committed or changed; its content lives in the row.
    DISPATCH = "agentplane_dispatcher_dispatch"


def create_wakeups(database_url: URL) -> WakeupListener:
    return WakeupListener(database_url, channels=Channel, application_name="agentplane-dispatcher-wakeups")

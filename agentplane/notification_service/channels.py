"""The Notification Service's NOTIFY channel: an invalidation, never the durable delivery."""

from enum import StrEnum

from sqlalchemy.engine import URL

from agentplane.postgres.wakeups import Wakeups


class Channel(StrEnum):
    # Inbox work was committed; its content and schedule live in the rows.
    WORK = "agentplane_notification_work"


def create_wakeups(database_url: URL) -> Wakeups:
    return Wakeups(database_url, channels=Channel, application_name="agentplane-notification-wakeups")

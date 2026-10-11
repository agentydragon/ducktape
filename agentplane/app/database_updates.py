"""The app's NOTIFY channels, on one LISTEN connection per replica."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.engine import URL

from agentplane.postgres.wakeups import Wakeups


class Channel(StrEnum):
    """What a replica listens for. A notification carries no payload -- no thread and no identity --
    so its readers re-read what they follow."""

    # A thread or its recorded resume was written. The History Service's fold commits wake the same
    # readers through `HistoryWatch`, which notifies this channel's `Changes` without a NOTIFY.
    THREADS = "agentplane_thread_updates"
    # A browser session row was deleted: logout, the rotation a login makes, or expiry cleanup.
    OPERATOR_SESSIONS = "agentplane_operator_sessions"


def create_database_updates(database_url: URL) -> Wakeups:
    return Wakeups(database_url, channels=Channel, application_name="agentplane-database-updates")

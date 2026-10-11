"""The History Service's NOTIFY channels, on one LISTEN connection per replica."""

from enum import StrEnum

from sqlalchemy.engine import URL

from agentplane.postgres.wakeups import Wakeups


class Channel(StrEnum):
    # Raw Session history was committed.
    HISTORY = "agentplane_session_history_changes"
    # The fold committed Session summaries.
    SUMMARIES = "agentplane_history_summary_changes"


def create_wakeups(database_url: URL) -> Wakeups:
    return Wakeups(database_url, channels=Channel, application_name="agentplane-history-changes")

"""History Service NOTIFY channels that Python code raises, matching `projection.ChannelSummaries` in Go."""

from enum import StrEnum


class Channel(StrEnum):
    # The fold committed Session summaries.
    SUMMARIES = "agentplane_history_summary_changes"

"""`ChannelSurface` — the last resident of a dissolving grab-bag.

Stable-side because <database_schema.py> reads it for the `channel_attachment.surface` column while
its target home under `channels/` sits above the schema — moving it there today would import-cycle
through `database_schema.py`. It waits for the channels/ packaging that gives it a leaf home.
"""

# CLEANUP(added 2026-08-28): last transitional resident of this grab-bag; delete the module once
#   `ChannelSurface` moves to `channels/` with the channel packaging (docs/naming_and_layout.md §6 C4).

from enum import StrEnum


class ChannelSurface(StrEnum):
    """Which channel holds a copy of a conversation.

    A row exists only for a channel that keeps a copy the console owes work against, so a browser
    tab is not a surface here and `ck_channel_attachment_surface` admits only `matrix`. Naming the
    channel keeps a replaced conversation findable by what held it.
    """

    MATRIX = "matrix"

"""Value domains for the session tables — a dissolving grab-bag.

Stable-side because <database_schema.py> reads these for its columns while their target read/event
modules sit above the schema — moving one there today would import-cycle through
`database_schema.py`; each waits for the reshape that gives it a leaf home.
"""

# CLEANUP(added 2026-08-28): transitional grab-bag, dissolved enum-by-enum
#   (docs/naming_and_layout.md §6 C4). Delete the module once the last vocabulary leaves:
#   the item enums with C6, `RuntimeKind` with C4d, `ChannelSurface` with the channels/ packaging.

from enum import StrEnum


class RuntimeKind(StrEnum):
    """Which concrete runner implementation a conversation is pinned to.

    Stored as text plus an ordinary CHECK rather than as a PostgreSQL enum. The application enum
    keeps readers closed, while widening the database constraint for the next implementation is a
    transactional migration instead of a PostgreSQL enum-type lifecycle.
    """

    CLAUDE_CODE = "claude_code"
    CODEX_APP_SERVER = "codex_app_server"


class ChannelSurface(StrEnum):
    """Which channel holds a copy of a conversation.

    A row exists only for a channel that keeps a copy the console owes work against, so a browser
    tab is not a surface here and `ck_channel_attachment_surface` admits only `matrix`. Naming the
    channel keeps a replaced conversation findable by what held it.
    """

    MATRIX = "matrix"


class ItemType(StrEnum):
    """What kind of thing an item is.

    A **decision** vocabulary (<README.md> § Vocabularies across a roll): every reader branches on
    it to know which of the per-type columns mean anything, so no reader-side answer is correct for
    a member it does not have and a new one ships a release behind its reader.
    """

    PROMPT = "prompt"
    MESSAGE = "message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"


class ToolOutcome(StrEnum):
    """How a tool call went, in the harness vocabulary rather than any one tool's.

    `UNKNOWN` is a real outcome and not a missing one: a call whose answer the backend reported
    without saying whether it succeeded, which every harness protocol permits.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ItemStatus(StrEnum):
    """An item's lifecycle, and nothing else.

    What it replaces put a prompt's queue state and an answer's completeness in one enum, told
    apart only by `role`. The queue state is `conversation_prompt`'s now, where a queue belongs.
    """

    OPEN = "open"
    COMPLETE = "complete"
    FAILED = "failed"

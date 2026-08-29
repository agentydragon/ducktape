"""The conversation-item value domains: what an item is (`ItemType`), where it stands
(`ItemStatus`), and how a tool call resolved (`ToolOutcome`).

A pure leaf, like `harnesses/kind.py`: `database_schema.py` reads these for the `conversation_item`
columns (`item_type`, `status`, `outcome`) and their CHECKs, and `conversation_event.py` reads
`ItemType`/`ToolOutcome` beneath the schema, so this module imports nothing from the console.

Kept out of `item_reads.py` — the item read models, their nominal home — because that module imports
the ORM rows from `database_schema.py`, which in turn needs these enums for its columns: hosting them
there would close a `database_schema` ↔ `item_reads` import cycle. A pure leaf both layers import
avoids it.
"""

from enum import StrEnum


class ItemType(StrEnum):
    """What kind of thing an item is.

    A **decision** vocabulary (<../README.md> § Vocabularies across a roll): every reader branches on
    it to know which of the per-type columns mean anything, so no reader-side answer is correct for
    a member it does not have and a new one ships a release behind its reader.
    """

    PROMPT = "prompt"
    MESSAGE = "message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"


class ItemStatus(StrEnum):
    """An item's lifecycle, and nothing else.

    What it replaces put a prompt's queue state and an answer's completeness in one enum, told
    apart only by `role`. The queue state is `conversation_prompt`'s now, where a queue belongs.
    """

    OPEN = "open"
    COMPLETE = "complete"
    FAILED = "failed"


class ToolOutcome(StrEnum):
    """How a tool call went, in the harness vocabulary rather than any one tool's.

    `UNKNOWN` is a real outcome and not a missing one: a call whose answer the backend reported
    without saying whether it succeeded, which every harness protocol permits.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"

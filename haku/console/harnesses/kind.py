"""The harness a conversation is pinned to — the closed discriminator, in its leaf home.

Console-side residue of harness *selection* (naming_and_layout.md §2): the native client and
frame projection move runner-ward with #4667, leaving only which harness a conversation runs.
A pure leaf: `database_schema.py` reads it for the `conversation.harness_kind` column and its
CHECK, so it imports nothing from the console.
"""

from enum import StrEnum


class HarnessKind(StrEnum):
    """Which concrete runner implementation a conversation is pinned to.

    Stored as text plus an ordinary CHECK rather than as a PostgreSQL enum. The application enum
    keeps readers closed, while widening the database constraint for the next implementation is a
    transactional migration instead of a PostgreSQL enum-type lifecycle.
    """

    CLAUDE_CODE = "claude_code"
    CODEX_APP_SERVER = "codex_app_server"

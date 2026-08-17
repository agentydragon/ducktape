"""Index the rollout by kind, because recording the deltas made scanning it by kind expensive.

`rollout_calls` reads every `assistant` and `user` frame of a session and re-parses their content
blocks, and it runs **per stream delta** — `update_assistant` NOTIFYs, `_sse_stream` wakes, and the
whole session view is rebuilt. That was already O(session) per token batch. Recording the deltas
multiplies the rows it has to scan past by roughly the length of an answer, which turns a known
inefficiency into a quadratic one while the SPA is streaming.

This does not fix the O(session) re-read — that wants incremental indexing on the agent's message
id. It removes the growth, by letting the read touch only the rows it wants instead of filtering
the session's whole log.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # `frame_seq` last so the index also serves the `ORDER BY` the read asks for, rather than
    # leaving it a sort on top of an index scan.
    op.create_index("idx_claude_chat_frames_kind", "claude_chat_frames", ["session_id", "kind", "frame_seq"])


def downgrade() -> None:
    op.drop_index("idx_claude_chat_frames_kind", table_name="claude_chat_frames")

"""The maintenance-gated generation cut to the neutral-operation transport (#4667 stage 4).

**MERGING THE PR THAT ADDS THIS MIGRATION ARMS THE CUT.** It runs on the next deploy's migration
Job, and it **refuses to apply while any session is live** — the assertions below RAISE inside the
transaction, so nothing changes and the deploy's migration step fails until the window is drained.
Merging it is therefore the act of scheduling the window: drain by simply not using the app, deploy
(this cuts or refuses), roll the images, run the health gate. The full runbook is
<../../docs/generation_cutover_runbook.md>. The cut's safety is the exact-generation peering
carried by the images themselves — an old bridge-v3 peer finds no common protocol version, and a
v4 peer of another generation fails the journal hello — so the schema holds no switch.

What one transaction does, per issue #4667 comment 5422375226 as amended by comment 5438689552:

1. **Freeze + assert.** Refuse the cut unless there are zero live sessions, zero open turns, zero
   claimed runner sandboxes, and zero pending runner commands (the old `conversation_prompt` queue
   and the new `submitted_prompt` inbox both empty of pending rows). Any remnant RAISEs, and the
   transaction rolls back with nothing done.
2. **Mark legacy sessions non-launchable.** After the assert only idle (unclaimed) sessions can
   remain; close them so no pre-cut session is resumed under the new protocol. Terminal/history rows
   stay readable. **`session_frames` is deliberately untouched** (comment 5438689552): the frames
   table stays durable for new sessions, keyed by runner frame seq, record-only — only its
   projection-input role retires, and that retirement is code, not schema.

The stage-3 columns and the `submitted_prompt` inbox already exist (0106); this migration flips no
schema those depend on, only closes the door on legacy launches.

Revision ID: 0109
Revises: 0108
"""

from __future__ import annotations

import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0109"
down_revision: str | None = "0108"
branch_labels: str | None = None
depends_on: str | None = None

# Each assertion: a human name, and the SQL counting what must be zero for the cut to be safe. Run
# in one transaction before any change, so a non-zero count rolls the whole migration back.
_FREEZE_ASSERTIONS: tuple[tuple[str, str], ...] = (
    (
        "live sessions (a runner sandbox is still claimed)",
        "SELECT count(*) FROM sessions WHERE ended_at IS NULL AND bridge_token_fingerprint IS NOT NULL",
    ),
    ("open turns", "SELECT count(*) FROM conversation_turn WHERE ended_at IS NULL"),
    (
        "uncleaned runner sandbox claims",
        "SELECT count(*) FROM sessions WHERE ended_at IS NOT NULL AND claim_cleaned_at IS NULL",
    ),
    (
        "pending runner commands (legacy conversation_prompt queue)",
        "SELECT count(*) FROM conversation_prompt WHERE claimed_at IS NULL",
    ),
    (
        "pending runner commands (submitted_prompt inbox)",
        "SELECT count(*) FROM submitted_prompt WHERE admitted_at IS NULL AND withdrawn_at IS NULL",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    remaining = [
        (name, count) for name, sql in _FREEZE_ASSERTIONS if (count := bind.execute(sa.text(sql)).scalar_one()) > 0
    ]
    if remaining:
        detail = "; ".join(f"{name}: {count}" for name, count in remaining)
        raise RuntimeError(
            "the generation cut refuses to apply while the window is not drained — "
            f"stop using the app and wait for these to clear, then re-deploy: {detail}"
        )

    # Legacy sessions non-launchable: only idle (unclaimed) sessions can remain past the assert;
    # close them so none is resumed under the new protocol. A clean close — no error — leaves them
    # readable history.
    op.execute(
        sa.text("UPDATE sessions SET ended_at = :now, updated_at = :now WHERE ended_at IS NULL").bindparams(
            now=datetime.datetime.now(datetime.UTC)
        )
    )

    # Let a runner number ride both directions of `session_frames`. Under the neutral-operation
    # generation the runner numbers the native input it injects itself and echoes it as a `to_agent`
    # frame carrying that seq, which the v3 constraint forbade. This changes the frames table's
    # *constraint*, never its role or data: it stays durable, keyed by the runner frame seq, exactly
    # as amendment 5438689552 requires (the frames table is not marked legacy).
    op.drop_constraint("ck_session_frames_runner_seq_direction", "session_frames", type_="check")

    # Repoint Matrix ingress dedup at the inbox. Under the neutral-operation generation a prompt is a
    # `submitted_prompt` command before it is any transcript item, so the dedup pointer moves from
    # `conversation_item` to `submitted_prompt`. **DROPS the dedup rows** — a conversation-scoped
    # table, covered by the standing allowance (<../../AGENTS.md> § Conversation data may be
    # dropped): re-delivery after the cut is a clean first offer into the inbox, harmless because the
    # freeze already required an empty inbox. `mcp_tool_calls`/audit are untouched.
    op.drop_table("matrix_ingress_event")
    op.create_table(
        "matrix_ingress_event",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("prompt_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="matrix_ingress_event_pkey"),
        sa.ForeignKeyConstraint(
            ["prompt_id"],
            ["submitted_prompt.prompt_id"],
            name="matrix_ingress_event_prompt_id_fkey",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_matrix_ingress_event_prompt", "matrix_ingress_event", ["prompt_id"])


def downgrade() -> None:
    # The generation cut is one-way by construction: the images that follow it speak only the new
    # protocol, so a schema downgrade cannot un-cut a running system. Restoring the frame-direction
    # constraint and the ingress table's old shape is the whole of what is reversible here;
    # recovery from a failed cut is the runbook's DB restore, not this.
    op.drop_index("idx_matrix_ingress_event_prompt", table_name="matrix_ingress_event")
    op.drop_table("matrix_ingress_event")
    op.create_table(
        "matrix_ingress_event",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("item_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="matrix_ingress_event_pkey"),
        sa.ForeignKeyConstraint(
            ["item_id"], ["conversation_item.item_id"], name="matrix_ingress_event_item_id_fkey", ondelete="CASCADE"
        ),
    )
    op.create_index("idx_matrix_ingress_event_item", "matrix_ingress_event", ["item_id"])
    op.create_check_constraint(
        "ck_session_frames_runner_seq_direction", "session_frames", "runner_seq IS NULL OR direction = 'from_agent'"
    )

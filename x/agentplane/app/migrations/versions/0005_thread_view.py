"""Materialize the derived Thread view: epochs, Segments and command summaries.

Everything here is rebuildable from the `event` archive, which stays the only source of truth. Two
deliberate absences: a Segment whose content is a carried Event stores no content, because the
archive already holds that Event at the same cursor; and there is no journal of derived updates,
because "what changed since H" is a query on `revision_cursor`.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_thread_view"
down_revision = "0004_followable_event_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projection_epoch",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("epoch", postgresql.UUID(as_uuid=True), primary_key=True),
        # The runner journal these cursors belong to. A change is an integrity condition.
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("through_cursor", sa.BigInteger(), nullable=False),
        # Serialized Controls, which are latest-wins over the folded prefix.
        sa.Column("controls", sa.LargeBinary(), nullable=False),
    )
    # A rebuild folds a second epoch forward beside the live one, so which epoch reads see is its own
    # row: selecting the rebuilt one is a single UPDATE and therefore atomic.
    op.create_table(
        "thread_projection",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("selected_epoch", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["thread_id", "selected_epoch"], ["projection_epoch.thread_id", "projection_epoch.epoch"]
        ),
    )
    op.create_table(
        "view_segment",
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("epoch", postgresql.UUID(as_uuid=True), primary_key=True),
        # The Segment's identity: the cursor of the Event that created it.
        sa.Column("cursor", sa.BigInteger(), primary_key=True),
        sa.Column("revision_cursor", sa.BigInteger(), nullable=False),
        # NULL means this Segment is the archived Event at `cursor`; only a folded Item is stored.
        sa.Column("item", sa.LargeBinary()),
        # The Item's id, so a later delta finds the Segment its Item started at without scanning. A
        # key extracted from the blob, the way `cursor` and `revision_cursor` are.
        sa.Column("item_id", sa.Text()),
        sa.ForeignKeyConstraint(
            ["thread_id", "epoch"], ["projection_epoch.thread_id", "projection_epoch.epoch"], ondelete="CASCADE"
        ),
    )
    # Catching up a reconnected caller is a range scan here, which is why no journal is stored.
    op.create_index("view_segment_revision", "view_segment", ["thread_id", "epoch", "revision_cursor"])
    op.create_index(
        "view_segment_item",
        "view_segment",
        ["thread_id", "epoch", "item_id"],
        unique=True,
        postgresql_where=sa.text("item_id IS NOT NULL"),
    )
    op.create_table(
        "view_command",
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("epoch", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("command_id", sa.Text(), primary_key=True),
        sa.Column("admission_cursor", sa.BigInteger(), nullable=False),
        # NULL while pending. The cursor that settled it is a fact; a status column would be a cache
        # of this that can disagree with the summary beside it.
        sa.Column("outcome_cursor", sa.BigInteger()),
        sa.Column("summary", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["thread_id", "epoch"], ["projection_epoch.thread_id", "projection_epoch.epoch"], ondelete="CASCADE"
        ),
    )
    op.create_index("view_command_admission", "view_command", ["thread_id", "epoch", "admission_cursor"])
    op.create_index(
        "view_command_pending",
        "view_command",
        ["thread_id", "epoch", "admission_cursor"],
        postgresql_where=sa.text("outcome_cursor IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("view_command")
    op.drop_table("view_segment")
    op.drop_table("thread_projection")
    op.drop_table("projection_epoch")

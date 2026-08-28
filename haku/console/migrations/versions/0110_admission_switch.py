"""The operator's prompt-admission switch, split out of the generation cut (#4667).

Creates the singleton ``runtime_control`` row post-cut: ``generation`` names the active transport
generation for the Console's image-ahead-of-migration fail-safe, and ``admission_closed`` is the
operator's drain switch, landing **open** (the steady state). Purely additive — the cut itself
(0109) neither needs nor reads it: peering safety is carried by the images (protocol-version
intersection plus the journal hello's generation).

Revision ID: 0110
Revises: 0109
"""

from __future__ import annotations

import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0110"
down_revision: str | None = "0109"
branch_labels: str | None = None
depends_on: str | None = None

# Must equal `haku.runtime.x.bridge.neutral_operations.GENERATION` — the value a runner presents on
# its journal hello — which `haku/console/x/test_runtime_control.py` pins so the two cannot drift.
GENERATION = "runner_projection_v1"


def upgrade() -> None:
    op.create_table(
        "runtime_control",
        sa.Column("id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("generation", sa.Text(), nullable=False),
        sa.Column("admission_closed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="runtime_control_pkey"),
        sa.CheckConstraint("id = 1", name="ck_runtime_control_singleton"),
        sa.CheckConstraint("btrim(generation) <> ''", name="ck_runtime_control_generation_nonempty"),
    )
    op.execute(
        sa.text(
            "INSERT INTO runtime_control (id, generation, admission_closed, updated_at)"
            " VALUES (1, :generation, false, :now)"
        ).bindparams(generation=GENERATION, now=datetime.datetime.now(datetime.UTC))
    )


def downgrade() -> None:
    op.drop_table("runtime_control")

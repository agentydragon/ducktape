"""Nothing watches Sessions any more: every runner pushes its Events to the History ingress."""

import sqlalchemy as sa
from alembic import op

revision = "0008_drop_session_feed"
down_revision = "0007_redelivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ux_session_change_position", table_name="session")
    op.drop_column("session", "change_position")
    op.execute(sa.schema.DropSequence(sa.Sequence("session_change_position")))


def downgrade() -> None:
    # Positions restart in table order; a watcher resumes from zero.
    op.execute(sa.schema.CreateSequence(sa.Sequence("session_change_position")))
    op.add_column(
        "session",
        sa.Column(
            "change_position",
            sa.BigInteger(),
            server_default=sa.text("nextval('session_change_position')"),
            nullable=False,
        ),
    )
    op.alter_column("session", "change_position", server_default=None)
    op.create_index("ux_session_change_position", "session", ["change_position"], unique=True)

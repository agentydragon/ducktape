"""Bind OAuth consent to one browser, operator, selection and token-family exchange."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_connection_enrollments"
down_revision = "0009_action_external_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connection_enrollment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("handle_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=True),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=False),
        sa.Column("upstream_url", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("browser_hash", sa.Text(), nullable=True),
        sa.Column("operator_issuer", sa.Text(), nullable=True),
        sa.Column("operator_subject", sa.Text(), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=True),
        sa.Column("decision_digest", sa.Text(), nullable=True),
        sa.Column("identity_id", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("exchange_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("client_id", "redirect_uri", "code_challenge"),
    )


def downgrade() -> None:
    op.drop_table("connection_enrollment")

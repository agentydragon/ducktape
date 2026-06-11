"""Initial schema

Revision ID: 001
Create Date: 2025-05-18

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # WebhookIntegration table
    op.create_table(
        "webhook_integrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("auth_type", sa.String(), nullable=False),
        sa.Column("auth_config", JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, default=True),
        sa.UniqueConstraint("name", name="uq_webhook_integrations_name"),
    )

    # WebhookPayload table
    op.create_table(
        "webhook_payloads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("received_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("integration_name", sa.String(), nullable=False),
        sa.Column("integration_id", sa.Integer(), sa.ForeignKey("webhook_integrations.id"), nullable=True),
        sa.Column("payload", JSON, nullable=False),
    )

    # AuthKey table
    op.create_table(
        "auth_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key_value", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("valid_duration_seconds", sa.Integer(), nullable=False, server_default="31536000"),  # 1 year
        sa.UniqueConstraint("key_value", name="uq_auth_keys_key_value"),
    )

    # AuthCRSession table
    op.create_table(
        "auth_cr_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_token", sa.String(), nullable=False),
        sa.Column("auth_key_id", sa.Integer(), sa.ForeignKey("auth_keys.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("session_token", name="uq_auth_cr_sessions_session_token"),
    )

    # AuthNonce table
    op.create_table(
        "auth_nonces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nonce_value", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("nonce_value", name="uq_auth_nonces_nonce_value"),
    )

    # Create indexes
    op.create_index("idx_webhook_payloads_received_at", "webhook_payloads", ["received_at"])
    op.create_index("idx_webhook_payloads_integration_name", "webhook_payloads", ["integration_name"])
    op.create_index("idx_auth_keys_revoked_at", "auth_keys", ["revoked_at"])
    op.create_index("idx_auth_cr_sessions_expires_at", "auth_cr_sessions", ["expires_at"])
    op.create_index("idx_auth_nonces_expires_at", "auth_nonces", ["expires_at"])
    op.create_index("idx_auth_nonces_used_at", "auth_nonces", ["used_at"])


def downgrade() -> None:
    op.drop_table("auth_nonces")
    op.drop_table("auth_cr_sessions")
    op.drop_table("auth_keys")
    op.drop_table("webhook_payloads")
    op.drop_table("webhook_integrations")

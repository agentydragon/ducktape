"""Normalize WebhookPayload - remove integration_name denormalization

Revision ID: 002
Revises: 001
Create Date: 2025-11-16

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the index on integration_name
    op.drop_index("idx_webhook_payloads_integration_name", table_name="webhook_payloads")

    # Drop the integration_name column
    op.drop_column("webhook_payloads", "integration_name")

    # Make integration_id NOT NULL
    op.alter_column("webhook_payloads", "integration_id", nullable=False)


def downgrade() -> None:
    # Make integration_id nullable again
    op.alter_column("webhook_payloads", "integration_id", nullable=True)

    # Add back the integration_name column
    op.add_column("webhook_payloads", sa.Column("integration_name", sa.String(), nullable=True))

    # Populate integration_name from the relationship
    op.execute("""
        UPDATE webhook_payloads
        SET integration_name = webhook_integrations.name
        FROM webhook_integrations
        WHERE webhook_payloads.integration_id = webhook_integrations.id
    """)

    # Make integration_name NOT NULL
    op.alter_column("webhook_payloads", "integration_name", nullable=False)

    # Recreate the index
    op.create_index("idx_webhook_payloads_integration_name", "webhook_payloads", ["integration_name"])

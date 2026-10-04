"""Standardize the subscription key spelling without changing identities or stored content."""

from alembic import op

revision = "0002_idempotency_key"
down_revision = "0001_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("subscription", "client_key", new_column_name="idempotency_key")
    op.execute(
        "ALTER TABLE subscription RENAME CONSTRAINT subscription_inbox_id_client_key_key "
        "TO subscription_inbox_id_idempotency_key_key"
    )
    op.execute(
        "UPDATE subscription SET creation = (creation - 'client_key') "
        "|| jsonb_build_object('idempotency_key', creation -> 'client_key') "
        "WHERE creation ? 'client_key'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE subscription SET creation = (creation - 'idempotency_key') "
        "|| jsonb_build_object('client_key', creation -> 'idempotency_key') "
        "WHERE creation ? 'idempotency_key'"
    )
    op.execute(
        "ALTER TABLE subscription RENAME CONSTRAINT subscription_inbox_id_idempotency_key_key "
        "TO subscription_inbox_id_client_key_key"
    )
    op.alter_column("subscription", "idempotency_key", new_column_name="client_key")

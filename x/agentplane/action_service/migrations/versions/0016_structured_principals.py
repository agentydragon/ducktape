"""Store a principal as its own fields, not as one formatted string.

A caller is a namespaced ServiceAccount and an operator is an issuer and a subject; both were
flattened into `<issuer>:<subject>` so one column could hold either. They no longer share a column,
so each column holds what its principal is. `action_decision.issuer` keeps a scalar, because a
deciding provider's name shares that column and it is part of the decision's uniqueness key.

Existing rows carry the old encoding and this service's history is diagnostic, so `action_request`
is emptied rather than parsed back apart; its Decisions, Executions, events and push deliveries
cascade with it. Subscriptions and linkage flows go for the same reason: a browser re-subscribes,
and a flow is re-entered on the next link.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_structured_principals"
down_revision = "0015_action_request_context"
branch_labels = None
depends_on = None

_OPERATOR_TABLES = ("action_push_subscription", "mcp_linkage_flow")


def upgrade() -> None:
    for table in ("action_request", *_OPERATOR_TABLES):
        op.execute(f"DELETE FROM {table}")

    op.drop_index("ix_action_request_caller_created", table_name="action_request")
    op.drop_constraint("action_request_caller_principal_idempotency_key_key", "action_request", type_="unique")
    op.drop_column("action_request", "caller_principal")
    op.add_column("action_request", sa.Column("caller_namespace", sa.Text(), nullable=False))
    op.add_column("action_request", sa.Column("caller_name", sa.Text(), nullable=False))
    op.create_unique_constraint(
        "action_request_caller_idempotency_key",
        "action_request",
        ["caller_namespace", "caller_name", "idempotency_key"],
    )
    op.create_index(
        "ix_action_request_caller_created", "action_request", ["caller_namespace", "caller_name", "created_at"]
    )

    op.drop_column("action_event", "actor_principal")
    op.add_column("action_event", sa.Column("actor", postgresql.JSONB(), nullable=True))

    op.drop_index("ix_action_push_subscription_operator_principal", table_name="action_push_subscription")
    for table in _OPERATOR_TABLES:
        op.drop_column(table, "operator_principal")
        op.add_column(table, sa.Column("operator_issuer", sa.Text(), nullable=False))
        op.add_column(table, sa.Column("operator_subject", sa.Text(), nullable=False))
    op.create_index("ix_action_push_subscription_operator_subject", "action_push_subscription", ["operator_subject"])


def downgrade() -> None:
    """Restore the old spelling rather than dropping rows: `<issuer>:<subject>`, with the caller's
    issuer being the constant this service used for every ServiceAccount. Going back is a faithful
    re-encoding; it is going forward that does not carry the old values, since a formatted string is
    what this migration exists to stop reading."""
    op.drop_index("ix_action_push_subscription_operator_subject", table_name="action_push_subscription")
    for table in _OPERATOR_TABLES:
        op.add_column(table, sa.Column("operator_principal", sa.Text(), nullable=True))
        op.execute(f"UPDATE {table} SET operator_principal = operator_issuer || ':' || operator_subject")
        op.alter_column(table, "operator_principal", nullable=False)
        op.drop_column(table, "operator_issuer")
        op.drop_column(table, "operator_subject")
    op.create_index(
        "ix_action_push_subscription_operator_principal", "action_push_subscription", ["operator_principal"]
    )

    op.add_column("action_event", sa.Column("actor_principal", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE action_event
        SET actor_principal = CASE
            WHEN actor ? 'account'
            THEN 'service-account:' || (actor -> 'account' ->> 'namespace') || ':' || (actor -> 'account' ->> 'name')
            ELSE (actor ->> 'issuer') || ':' || (actor ->> 'subject')
        END
        WHERE actor IS NOT NULL
        """
    )
    op.drop_column("action_event", "actor")

    op.drop_index("ix_action_request_caller_created", table_name="action_request")
    op.drop_constraint("action_request_caller_idempotency_key", "action_request", type_="unique")
    op.add_column("action_request", sa.Column("caller_principal", sa.Text(), nullable=True))
    op.execute(
        "UPDATE action_request SET caller_principal = 'service-account:' || caller_namespace || ':' || caller_name"
    )
    op.alter_column("action_request", "caller_principal", nullable=False)
    op.drop_column("action_request", "caller_namespace")
    op.drop_column("action_request", "caller_name")
    op.create_unique_constraint(
        "action_request_caller_principal_idempotency_key_key", "action_request", ["caller_principal", "idempotency_key"]
    )
    op.create_index("ix_action_request_caller_created", "action_request", ["caller_principal", "created_at"])

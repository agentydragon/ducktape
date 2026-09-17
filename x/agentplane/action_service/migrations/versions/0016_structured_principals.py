"""Store a principal as its own fields, not as one formatted string.

A caller is a namespaced ServiceAccount and an operator is an issuer and a subject; both were
flattened into `<issuer>:<subject>` so one column could hold either. They no longer share a column,
so each column holds what its principal is. `action_decision.issuer` keeps a scalar, because a
deciding provider's name shares that column and it is part of the decision's uniqueness key.

Neither direction carries the rows over. This service's history is diagnostic, a browser
re-subscribes and a linkage flow is re-entered on the next link, so `action_request`, the
subscriptions and the flows are emptied and `caller_principal` / `operator_principal` come back
empty on the way down. Decisions, Executions, events and push deliveries cascade with the requests.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_structured_principals"
down_revision = "0015_action_request_context"
branch_labels = None
depends_on = None

_OPERATOR_TABLES = ("action_push_subscription", "mcp_linkage_flow")
_EMPTIED = ("action_request", *_OPERATOR_TABLES)


def upgrade() -> None:
    for table in _EMPTIED:
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
    for table in _EMPTIED:
        op.execute(f"DELETE FROM {table}")

    op.drop_index("ix_action_push_subscription_operator_subject", table_name="action_push_subscription")
    for table in _OPERATOR_TABLES:
        op.drop_column(table, "operator_issuer")
        op.drop_column(table, "operator_subject")
        op.add_column(table, sa.Column("operator_principal", sa.Text(), nullable=False))
    op.create_index(
        "ix_action_push_subscription_operator_principal", "action_push_subscription", ["operator_principal"]
    )

    op.drop_column("action_event", "actor")
    op.add_column("action_event", sa.Column("actor_principal", sa.Text(), nullable=True))

    op.drop_index("ix_action_request_caller_created", table_name="action_request")
    op.drop_constraint("action_request_caller_idempotency_key", "action_request", type_="unique")
    op.drop_column("action_request", "caller_namespace")
    op.drop_column("action_request", "caller_name")
    op.add_column("action_request", sa.Column("caller_principal", sa.Text(), nullable=False))
    op.create_unique_constraint(
        "action_request_caller_principal_idempotency_key_key", "action_request", ["caller_principal", "idempotency_key"]
    )
    op.create_index("ix_action_request_caller_created", "action_request", ["caller_principal", "created_at"])

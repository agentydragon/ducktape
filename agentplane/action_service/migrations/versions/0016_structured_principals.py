"""Every principal is stored as its own fields, not as one formatted string.

A caller was `service-account:<namespace>:<name>` and an operator `<issuer>:<subject>`, flattened so
one column could hold either. They no longer share a column, so each column holds what its principal
is: a caller is a namespaced ServiceAccount, an operator an issuer and a subject.

`action_decision` had a second problem in the same column. `issuer` held either the operator behind
a human Decision or -- for a DecisionProvider's -- a second copy of `provider`. The variants get
their own columns, with a check constraint keeping the operator pair whole and a `NULLS NOT
DISTINCT` uniqueness key so a provider's all-NULL rows still collide with each other; under
Postgres' default they never would, exempting exactly them from the replay backstop.

Neither direction carries rows over. This service's history is diagnostic, a browser re-subscribes
and a linkage flow is re-entered on the next link, so `action_request`, the subscriptions and the
flows are emptied and the old columns come back empty on the way down. Decisions, Executions, events
and push deliveries cascade with the requests. `mcp_server_linkage` keeps its rows: they carry the
shared OAuth token state, and `linked_by` is nullable provenance beside it.
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
_OPERATOR_WHOLE = "(operator_issuer IS NULL) = (operator_subject IS NULL)"
_LINKED_BY_WHOLE = "(linked_by_issuer IS NULL) = (linked_by_subject IS NULL)"


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

    op.drop_constraint("action_decision_provider_issuer_idempotency_key_key", "action_decision", type_="unique")
    op.drop_column("action_decision", "issuer")
    op.add_column("action_decision", sa.Column("operator_issuer", sa.Text(), nullable=True))
    op.add_column("action_decision", sa.Column("operator_subject", sa.Text(), nullable=True))
    op.create_check_constraint("action_decision_operator_whole", "action_decision", _OPERATOR_WHOLE)
    op.create_unique_constraint(
        "action_decision_provider_operator_idempotency_key",
        "action_decision",
        ["provider", "operator_issuer", "operator_subject", "idempotency_key"],
        postgresql_nulls_not_distinct=True,
    )

    op.drop_index("ix_action_push_subscription_operator_principal", table_name="action_push_subscription")
    for table in _OPERATOR_TABLES:
        op.drop_column(table, "operator_principal")
        op.add_column(table, sa.Column("operator_issuer", sa.Text(), nullable=False))
        op.add_column(table, sa.Column("operator_subject", sa.Text(), nullable=False))
    op.create_index("ix_action_push_subscription_operator_subject", "action_push_subscription", ["operator_subject"])

    op.drop_column("mcp_server_linkage", "linked_by")
    op.add_column("mcp_server_linkage", sa.Column("linked_by_issuer", sa.Text(), nullable=True))
    op.add_column("mcp_server_linkage", sa.Column("linked_by_subject", sa.Text(), nullable=True))
    op.create_check_constraint("mcp_server_linkage_linked_by_whole", "mcp_server_linkage", _LINKED_BY_WHOLE)


def downgrade() -> None:
    for table in _EMPTIED:
        op.execute(f"DELETE FROM {table}")

    op.drop_constraint("mcp_server_linkage_linked_by_whole", "mcp_server_linkage", type_="check")
    op.drop_column("mcp_server_linkage", "linked_by_issuer")
    op.drop_column("mcp_server_linkage", "linked_by_subject")
    op.add_column("mcp_server_linkage", sa.Column("linked_by", sa.Text(), nullable=True))

    op.drop_index("ix_action_push_subscription_operator_subject", table_name="action_push_subscription")
    for table in _OPERATOR_TABLES:
        op.drop_column(table, "operator_issuer")
        op.drop_column(table, "operator_subject")
        op.add_column(table, sa.Column("operator_principal", sa.Text(), nullable=False))
    op.create_index(
        "ix_action_push_subscription_operator_principal", "action_push_subscription", ["operator_principal"]
    )

    op.drop_constraint("action_decision_provider_operator_idempotency_key", "action_decision", type_="unique")
    op.drop_constraint("action_decision_operator_whole", "action_decision", type_="check")
    op.drop_column("action_decision", "operator_issuer")
    op.drop_column("action_decision", "operator_subject")
    op.add_column("action_decision", sa.Column("issuer", sa.Text(), nullable=False))
    op.create_unique_constraint(
        "action_decision_provider_issuer_idempotency_key_key",
        "action_decision",
        ["provider", "issuer", "idempotency_key"],
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

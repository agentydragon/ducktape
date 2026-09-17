"""Record a Decision's operator as its own columns, leaving `provider` to name what decided.

`issuer` held either the operator behind a human Decision, flattened to `<issuer>:<subject>`, or --
for a DecisionProvider's Decision -- a second copy of `provider`. The two variants now have separate
columns: an operator Decision sets both, a provider Decision sets neither, and a check constraint
keeps the pair whole.

The uniqueness key follows, and it is `NULLS NOT DISTINCT` on purpose: a provider Decision has both
columns NULL, and under Postgres' default those rows would never collide with each other, exempting
exactly them from the replay backstop.

`action_request` is emptied rather than migrated, as in `0016`. Reading an existing Decision's
operator out of the old string is possible; deciding which half of `<issuer>:<subject>` is which is
not, and a human Decision left with empty operator columns would read as a provider's.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_decision_operator_columns"
down_revision = "0017_linkage_linked_by_operator"
branch_labels = None
depends_on = None

_WHOLE = "(operator_issuer IS NULL) = (operator_subject IS NULL)"


def upgrade() -> None:
    op.execute("DELETE FROM action_request")
    op.drop_constraint("action_decision_provider_issuer_idempotency_key_key", "action_decision", type_="unique")
    op.drop_column("action_decision", "issuer")
    op.add_column("action_decision", sa.Column("operator_issuer", sa.Text(), nullable=True))
    op.add_column("action_decision", sa.Column("operator_subject", sa.Text(), nullable=True))
    op.create_check_constraint("action_decision_operator_whole", "action_decision", _WHOLE)
    op.create_unique_constraint(
        "action_decision_provider_operator_idempotency_key",
        "action_decision",
        ["provider", "operator_issuer", "operator_subject", "idempotency_key"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.execute("DELETE FROM action_request")
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

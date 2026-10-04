"""Nest the immutable provider configuration in subscription creation snapshots."""

from alembic import op

revision = "0004_source_union"
down_revision = "0003_remove_pause"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE subscription SET creation = (creation - 'provider' - 'request_id' - 'after_sequence') "
        "|| jsonb_build_object('source', jsonb_build_object("
        "'provider', creation -> 'provider', "
        "'request_id', creation -> 'request_id', "
        "'after_sequence', creation -> 'after_sequence'))"
    )

    # Fail stale writers closed rather than persisting snapshots the serving API cannot read.
    op.create_check_constraint("subscription_creation_source", "subscription", "creation ? 'source'")


def downgrade() -> None:
    op.drop_constraint("subscription_creation_source", "subscription", type_="check")
    op.execute("UPDATE subscription SET creation = (creation - 'source') || (creation -> 'source')")

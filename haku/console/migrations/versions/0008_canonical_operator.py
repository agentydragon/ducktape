"""Cut every live authority path over to canonical Operator UUIDs.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import ENUM, UUID

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATOR_STATUSES = ("active", "disabled")


def _operator_status_enum(*, create_type: bool = False) -> ENUM:
    return ENUM(*_OPERATOR_STATUSES, name="operator_status", create_type=create_type)


def _seed_configured_anchors() -> None:
    bind = op.get_bind()
    now = datetime.datetime.now(datetime.UTC)
    for trust_domain, stable_external_user_key in context.config.attributes.get("operator_identity_seeds", ()):
        operator_id = uuid4()
        anchor_id = uuid4()
        bind.execute(
            sa.text(
                """
                INSERT INTO operators (operator_id, status, created_at, updated_at)
                VALUES (:operator_id, 'active', :now, :now)
                """
            ),
            {"operator_id": operator_id, "now": now},
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO identity_anchors (
                    anchor_id, operator_id, trust_domain, stable_external_user_key, created_at, updated_at
                ) VALUES (:anchor_id, :operator_id, :trust_domain, :stable_external_user_key, :now, :now)
                """
            ),
            {
                "anchor_id": anchor_id,
                "operator_id": operator_id,
                "trust_domain": trust_domain,
                "stable_external_user_key": stable_external_user_key,
                "now": now,
            },
        )
        bind.execute(
            sa.text(
                """
                UPDATE mcp_operator_oauth_associations
                SET operator_id = :operator_id
                WHERE operator_subject = :stable_external_user_key
                """
            ),
            {"operator_id": operator_id, "stable_external_user_key": stable_external_user_key},
        )


def _invalidate_fastmcp_oauth_state() -> None:
    """Invalidate the complete pre-cutover downstream credential authority, when present.

    FastMCP's configured PostgreSQLStore is created lazily outside Alembic, so a fresh database can
    legitimately lack the table. When it exists it is dedicated to Haku's OAuth proxy: deleting all
    collections is deliberate and future-proof, rather than preserving an old token/JTI family that
    could inherit a newly linked canonical Operator.
    """
    table_name = context.config.attributes.get("fastmcp_oauth_state_table")
    if table_name is None:
        return
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table_name):
        return
    state_table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    bind.execute(sa.delete(state_table))


def upgrade() -> None:
    bind = op.get_bind()
    _operator_status_enum(create_type=True).create(bind, checkfirst=True)

    op.create_table(
        "operators",
        sa.Column("operator_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("status", _operator_status_enum(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "identity_anchors",
        sa.Column("anchor_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "operator_id",
            UUID(as_uuid=True),
            sa.ForeignKey("operators.operator_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trust_domain", sa.Text(), nullable=False),
        sa.Column("stable_external_user_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("btrim(trust_domain) <> ''", name="ck_identity_anchors_trust_domain_nonempty"),
        sa.CheckConstraint("btrim(stable_external_user_key) <> ''", name="ck_identity_anchors_external_key_nonempty"),
        sa.UniqueConstraint(
            "trust_domain", "stable_external_user_key", name="uq_identity_anchors_trust_domain_external_key"
        ),
    )
    op.create_index("idx_identity_anchors_operator_id", "identity_anchors", ["operator_id"])
    op.create_table(
        "oidc_identities",
        sa.Column("identity_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "anchor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("identity_anchors.anchor_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("btrim(issuer) <> ''", name="ck_oidc_identities_issuer_nonempty"),
        sa.CheckConstraint("btrim(subject) <> ''", name="ck_oidc_identities_subject_nonempty"),
        sa.UniqueConstraint("issuer", "subject", name="uq_oidc_identities_issuer_subject"),
    )
    op.create_index("idx_oidc_identities_anchor_id", "oidc_identities", ["anchor_id"])

    # Add temporary UUID columns while the legacy text keys remain available for exact matching.
    op.add_column("mcp_operator_oauth_associations", sa.Column("operator_id", UUID(as_uuid=True), nullable=True))
    op.add_column("mcp_agent_operator", sa.Column("operator_id", UUID(as_uuid=True), nullable=True))
    _seed_configured_anchors()

    # Only downstream backend-token associations matching a controller-fed stable external user
    # key survive. In-flight grants and historical ledger rows are never inferred or re-owned
    # across the identity boundary. FastMCP registrations/token families and their derived DCR
    # links are one authority graph, so invalidate both halves and require every OAuth agent to
    # authorize again, with fresh local registration where applicable, against canonical Operator
    # ownership.
    op.execute("DELETE FROM mcp_operator_oauth_associations WHERE operator_id IS NULL")
    _invalidate_fastmcp_oauth_state()
    op.execute("DELETE FROM mcp_agent_operator")
    op.execute("DELETE FROM mcp_operator_oauth_flows")
    op.execute("DELETE FROM mcp_tool_call_events")
    op.execute("DELETE FROM mcp_tool_calls")

    # A refresh releases its row lock while calling the upstream token endpoint. Give every
    # surviving association an immutable generation plus an incrementing token revision so the
    # final write can reject stale results after a concurrent refresh or disconnect/reconnect.
    op.add_column("mcp_operator_oauth_associations", sa.Column("association_id", UUID(as_uuid=True), nullable=True))
    op.add_column(
        "mcp_operator_oauth_associations",
        sa.Column("token_revision", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    for server_id, operator_subject in bind.execute(
        sa.text("SELECT server_id, operator_subject FROM mcp_operator_oauth_associations")
    ):
        bind.execute(
            sa.text(
                """
                UPDATE mcp_operator_oauth_associations
                SET association_id = :association_id
                WHERE server_id = :server_id AND operator_subject = :operator_subject
                """
            ),
            {"association_id": uuid4(), "server_id": server_id, "operator_subject": operator_subject},
        )
    op.alter_column("mcp_operator_oauth_associations", "association_id", nullable=False)
    op.alter_column("mcp_operator_oauth_associations", "token_revision", server_default=None)

    op.drop_index("idx_mcp_operator_oauth_associations_operator", table_name="mcp_operator_oauth_associations")
    op.drop_constraint("mcp_operator_oauth_associations_pkey", "mcp_operator_oauth_associations", type_="primary")
    op.drop_column("mcp_operator_oauth_associations", "operator_subject")
    op.alter_column("mcp_operator_oauth_associations", "operator_id", nullable=False)
    op.create_foreign_key(
        "fk_mcp_operator_oauth_associations_operator",
        "mcp_operator_oauth_associations",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="CASCADE",
    )
    op.create_primary_key(
        "mcp_operator_oauth_associations_pkey", "mcp_operator_oauth_associations", ["server_id", "operator_id"]
    )
    op.create_unique_constraint(
        "uq_mcp_operator_oauth_associations_association_id", "mcp_operator_oauth_associations", ["association_id"]
    )
    op.create_index("idx_mcp_operator_oauth_associations_operator", "mcp_operator_oauth_associations", ["operator_id"])

    op.drop_column("mcp_agent_operator", "operator_subject")
    op.alter_column("mcp_agent_operator", "operator_id", nullable=False)
    op.create_foreign_key(
        "fk_mcp_agent_operator_operator",
        "mcp_agent_operator",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="CASCADE",
    )

    op.drop_index("idx_mcp_operator_oauth_flows_server_operator", table_name="mcp_operator_oauth_flows")
    op.drop_column("mcp_operator_oauth_flows", "operator_subject")
    op.add_column("mcp_operator_oauth_flows", sa.Column("operator_id", UUID(as_uuid=True), nullable=False))
    op.create_foreign_key(
        "fk_mcp_operator_oauth_flows_operator",
        "mcp_operator_oauth_flows",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_mcp_operator_oauth_flows_server_operator", "mcp_operator_oauth_flows", ["server_id", "operator_id"]
    )

    op.drop_index("idx_mcp_tool_calls_operator_subject_created_at", table_name="mcp_tool_calls")
    op.drop_column("mcp_tool_calls", "operator_subject")
    op.add_column("mcp_tool_calls", sa.Column("operator_id", UUID(as_uuid=True), nullable=False))
    op.create_foreign_key(
        "fk_mcp_tool_calls_operator",
        "mcp_tool_calls",
        "operators",
        ["operator_id"],
        ["operator_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_mcp_tool_calls_id_operator", "mcp_tool_calls", ["tool_call_id", "operator_id"])
    op.create_index("idx_mcp_tool_calls_operator_id_created_at", "mcp_tool_calls", ["operator_id", "created_at"])

    op.drop_index("idx_mcp_tool_call_events_operator_subject_event_id", table_name="mcp_tool_call_events")
    op.drop_column("mcp_tool_call_events", "operator_subject")
    op.add_column("mcp_tool_call_events", sa.Column("operator_id", UUID(as_uuid=True), nullable=False))
    op.create_foreign_key(
        "fk_mcp_tool_call_events_call_owner",
        "mcp_tool_call_events",
        "mcp_tool_calls",
        ["tool_call_id", "operator_id"],
        ["tool_call_id", "operator_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_mcp_tool_call_events_operator_id_event_id", "mcp_tool_call_events", ["operator_id", "event_id"]
    )


def downgrade() -> None:
    raise RuntimeError("0008 is forward-only: canonical Operator authority cannot be converted back to bare subjects")

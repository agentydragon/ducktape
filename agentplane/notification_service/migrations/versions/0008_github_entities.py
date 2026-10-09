"""Normalize GitHub identities, repository grants, and subject associations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_github_entities"
down_revision = "0007_subscription_health"
branch_labels = None
depends_on = None


def refresh_columns() -> list[sa.Column]:
    return [
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("error_kind", sa.String()),
        sa.Column("error", sa.String()),
        sa.Column("error_since", sa.DateTime(timezone=True)),
        sa.Column("error_observed_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt", sa.DateTime(timezone=True)),
        sa.Column("claim", sa.Uuid()),
        sa.Column("claim_until", sa.DateTime(timezone=True)),
    ]


def refresh_constraints(prefix: str) -> tuple[sa.CheckConstraint, ...]:
    return (
        sa.CheckConstraint(
            "error_kind IN ('rate_limited', 'unavailable', 'access_denied', 'source_changed', 'processing_error')",
            name=f"{prefix}_error_kind",
        ),
        sa.CheckConstraint(
            "(error IS NULL AND error_kind IS NULL AND error_since IS NULL AND error_observed_at IS NULL) OR "
            "(error IS NOT NULL AND error_kind IS NOT NULL AND error_since IS NOT NULL AND error_observed_at IS NOT NULL)",
            name=f"{prefix}_error_state",
        ),
    )


def upgrade() -> None:
    op.create_table(
        "github_installation",
        sa.Column("app_id", sa.BigInteger(), primary_key=True),
        sa.Column("installation_id", sa.BigInteger(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "github_repository",
        sa.Column("repository_id", sa.BigInteger(), primary_key=True),
        sa.Column("full_name", sa.String(), nullable=False),
    )
    op.create_table(
        "github_repository_access",
        *refresh_constraints("github_access"),
        sa.Column("app_id", sa.BigInteger(), primary_key=True),
        sa.Column("installation_id", sa.BigInteger(), primary_key=True),
        sa.Column("repository_id", sa.BigInteger(), sa.ForeignKey("github_repository.repository_id"), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("validated_installation_generation", sa.BigInteger()),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        *refresh_columns(),
        sa.ForeignKeyConstraint(
            ["app_id", "installation_id"], ["github_installation.app_id", "github_installation.installation_id"]
        ),
    )
    op.create_table(
        "github_subject",
        *refresh_constraints("github_subject"),
        sa.Column("repository_id", sa.BigInteger(), sa.ForeignKey("github_repository.repository_id"), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("subject_key", sa.String(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        *refresh_columns(),
        sa.CheckConstraint("kind IN ('pull_request', 'branch', 'commit')", name="github_subject_kind"),
    )
    op.create_table(
        "github_subject_revision",
        sa.Column("repository_id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("subject_key", sa.String(), primary_key=True),
        sa.Column(
            "head_repository_id", sa.BigInteger(), sa.ForeignKey("github_repository.repository_id"), primary_key=True
        ),
        sa.Column("sha", sa.String(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["repository_id", "kind", "subject_key"],
            ["github_subject.repository_id", "github_subject.kind", "github_subject.subject_key"],
        ),
    )
    op.create_unique_constraint("github_delivery_position_repository", "github_delivery", ["position", "repository_id"])
    op.create_table(
        "github_delivery_subject",
        sa.Column("delivery_position", sa.BigInteger(), primary_key=True),
        sa.Column("repository_id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("subject_key", sa.String(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["delivery_position", "repository_id"],
            ["github_delivery.position", "github_delivery.repository_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "kind", "subject_key"],
            ["github_subject.repository_id", "github_subject.kind", "github_subject.subject_key"],
        ),
    )
    op.create_index(
        "ix_github_delivery_subject_lookup",
        "github_delivery_subject",
        ["repository_id", "kind", "subject_key", "delivery_position"],
    )
    op.drop_constraint("subscription_source_state", "subscription", type_="check")
    for name in ("github_app_id", "github_installation_id", "github_repository_id"):
        op.add_column("subscription", sa.Column(name, sa.BigInteger()))
    for name in ("github_subject_kind", "github_subject_key"):
        op.add_column("subscription", sa.Column(name, sa.String()))
    op.execute("""
        UPDATE subscription SET
            github_app_id = (github_binding->>'app_id')::bigint,
            github_installation_id = (github_binding->>'installation_id')::bigint,
            github_repository_id = (github_binding->>'repository_id')::bigint,
            github_subject_kind = creation #>> '{source,subject,kind}',
            github_subject_key = coalesce(creation #>> '{source,subject,number}',
                creation #>> '{source,subject,name}', creation #>> '{source,subject,sha}')
        WHERE github_binding IS NOT NULL
    """)
    op.execute(
        "INSERT INTO github_installation SELECT DISTINCT github_app_id, github_installation_id, 0 FROM subscription WHERE github_app_id IS NOT NULL"
    )
    op.execute(
        "INSERT INTO github_repository SELECT github_repository_id, min(creation #>> '{source,repository}') FROM subscription WHERE github_repository_id IS NOT NULL GROUP BY github_repository_id"
    )
    op.execute(
        "INSERT INTO github_repository_access (app_id, installation_id, repository_id, generation) SELECT DISTINCT github_app_id, github_installation_id, github_repository_id, 0 FROM subscription WHERE github_app_id IS NOT NULL"
    )
    op.execute(
        "INSERT INTO github_subject (repository_id, kind, subject_key, generation) SELECT DISTINCT github_repository_id, github_subject_kind, github_subject_key, 0 FROM subscription WHERE github_repository_id IS NOT NULL"
    )
    # Retained receipts can explain CI for heads no longer returned by the current GitHub API.
    op.execute("""
        INSERT INTO github_repository (repository_id, full_name)
        SELECT repository_id, min(payload #>> '{repository,full_name}') FROM github_delivery
        WHERE repository_id IS NOT NULL AND payload #>> '{repository,full_name}' IS NOT NULL
        GROUP BY repository_id ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO github_repository (repository_id, full_name)
        SELECT (payload #>> '{pull_request,head,repo,id}')::bigint,
            min(payload #>> '{pull_request,head,repo,full_name}') FROM github_delivery
        WHERE payload #>> '{pull_request,head,repo,id}' IS NOT NULL
            AND payload #>> '{pull_request,head,repo,full_name}' IS NOT NULL
        GROUP BY (payload #>> '{pull_request,head,repo,id}')::bigint ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO github_subject (repository_id, kind, subject_key, generation)
        SELECT DISTINCT d.repository_id, split_part(subject, ':', 1),
            substring(subject from position(':' in subject) + 1), 0
        FROM github_delivery d JOIN github_repository r ON r.repository_id = d.repository_id
        CROSS JOIN LATERAL unnest(d.subjects) AS subject
        WHERE split_part(subject, ':', 1) IN ('pull_request', 'branch')
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO github_delivery_subject (delivery_position, repository_id, kind, subject_key)
        SELECT DISTINCT d.position, s.repository_id, s.kind, s.subject_key
        FROM github_delivery d CROSS JOIN LATERAL unnest(d.subjects) AS subject
        JOIN github_subject s ON s.repository_id = d.repository_id
            AND s.kind = split_part(subject, ':', 1)
            AND s.subject_key = substring(subject from position(':' in subject) + 1)
    """)
    op.execute("""
        INSERT INTO github_subject_revision (repository_id, kind, subject_key, head_repository_id, sha)
        SELECT DISTINCT s.repository_id, s.kind, s.subject_key, d.repository_id, d.head_sha
        FROM github_delivery d JOIN github_delivery_subject s ON s.delivery_position = d.position
        WHERE d.head_sha IS NOT NULL ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO github_subject_revision (repository_id, kind, subject_key, head_repository_id, sha)
        SELECT DISTINCT s.repository_id, s.kind, s.subject_key,
            (d.payload #>> '{pull_request,head,repo,id}')::bigint, d.head_sha
        FROM github_delivery d JOIN github_delivery_subject s ON s.delivery_position = d.position
        JOIN github_repository r ON r.repository_id = (d.payload #>> '{pull_request,head,repo,id}')::bigint
        WHERE d.head_sha IS NOT NULL ON CONFLICT DO NOTHING
    """)
    op.drop_index("ix_github_delivery_subjects", table_name="github_delivery")
    op.drop_column("github_delivery", "subjects")
    op.drop_column("subscription", "github_binding")
    op.create_foreign_key(
        "subscription_github_access",
        "subscription",
        "github_repository_access",
        ["github_app_id", "github_installation_id", "github_repository_id"],
        ["app_id", "installation_id", "repository_id"],
    )
    op.create_foreign_key(
        "subscription_github_subject",
        "subscription",
        "github_subject",
        ["github_repository_id", "github_subject_kind", "github_subject_key"],
        ["repository_id", "kind", "subject_key"],
    )
    op.create_check_constraint(
        "subscription_source_state",
        "subscription",
        "CASE creation #>> '{source,provider}' "
        "WHEN 'actions' THEN actions_after_sequence IS NOT NULL AND github_start_position IS NULL "
        "AND github_app_id IS NULL AND github_installation_id IS NULL AND github_repository_id IS NULL "
        "AND github_subject_kind IS NULL AND github_subject_key IS NULL "
        "WHEN 'github' THEN actions_after_sequence IS NULL AND github_start_position IS NOT NULL "
        "AND github_app_id IS NOT NULL AND github_installation_id IS NOT NULL AND github_repository_id IS NOT NULL "
        "AND github_subject_kind IS NOT NULL AND github_subject_key IS NOT NULL ELSE false END",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM github_subject_revision) OR "
                "EXISTS (SELECT 1 FROM github_repository_access WHERE checked_at IS NOT NULL OR error IS NOT NULL) OR "
                "EXISTS (SELECT 1 FROM github_subject WHERE last_success_at IS NOT NULL OR error IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("refusing data loss: shared GitHub observations require a reverse migration")
    op.add_column(
        "github_delivery",
        sa.Column("subjects", postgresql.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'::varchar[]")),
    )
    op.execute("""
        UPDATE github_delivery d SET subjects = (
            SELECT array_agg(s.kind || ':' || s.subject_key ORDER BY s.kind, s.subject_key)::varchar[]
            FROM github_delivery_subject s WHERE s.delivery_position = d.position
        ) WHERE EXISTS (SELECT 1 FROM github_delivery_subject s WHERE s.delivery_position = d.position)
    """)
    op.alter_column("github_delivery", "subjects", server_default=None)
    op.create_index("ix_github_delivery_subjects", "github_delivery", ["subjects"], postgresql_using="gin")
    op.drop_table("github_delivery_subject")
    op.drop_constraint("github_delivery_position_repository", "github_delivery", type_="unique")
    op.drop_constraint("subscription_source_state", "subscription", type_="check")
    op.drop_constraint("subscription_github_subject", "subscription", type_="foreignkey")
    op.drop_constraint("subscription_github_access", "subscription", type_="foreignkey")
    op.add_column("subscription", sa.Column("github_binding", postgresql.JSONB(none_as_null=True)))
    op.execute(
        "UPDATE subscription SET github_binding = jsonb_build_object('app_id', github_app_id, 'installation_id', github_installation_id, 'repository_id', github_repository_id) WHERE github_app_id IS NOT NULL"
    )
    for column in (
        "github_app_id",
        "github_installation_id",
        "github_repository_id",
        "github_subject_kind",
        "github_subject_key",
    ):
        op.drop_column("subscription", column)
    op.create_check_constraint(
        "subscription_source_state",
        "subscription",
        "CASE creation #>> '{source,provider}' "
        "WHEN 'actions' THEN actions_after_sequence IS NOT NULL AND github_start_position IS NULL "
        "AND github_binding IS NULL "
        "WHEN 'github' THEN actions_after_sequence IS NULL AND github_start_position IS NOT NULL "
        "AND github_binding IS NOT NULL ELSE false END",
    )
    for table in (
        "github_subject_revision",
        "github_subject",
        "github_repository_access",
        "github_repository",
        "github_installation",
    ):
        op.drop_table(table)

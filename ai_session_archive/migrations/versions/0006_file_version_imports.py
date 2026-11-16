"""Keep restartable record batches outside the published version catalog."""

import sqlalchemy as sa
from alembic import op

revision = "0006_file_version_imports"
down_revision = "0005_codex_title_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_version_imports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(50), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("imported_record_count", sa.Integer(), nullable=False),
        sa.Column("parse_issue_count", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(), sa.ForeignKey("file_versions.id", ondelete="RESTRICT")),
        sa.Column("shared_prefix_record_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("session_id", "sha256", name="uq_file_version_imports_session_sha256"),
        sa.CheckConstraint("byte_size >= 0", name="ck_file_version_imports_byte_size"),
        sa.CheckConstraint(
            "imported_record_count >= 0 AND imported_record_count <= record_count",
            name="ck_file_version_imports_progress",
        ),
        sa.CheckConstraint(
            "parse_issue_count >= 0 AND parse_issue_count <= imported_record_count",
            name="ck_file_version_imports_issues",
        ),
        sa.CheckConstraint(
            "shared_prefix_record_count >= 0 AND shared_prefix_record_count <= imported_record_count",
            name="ck_file_version_imports_prefix",
        ),
    )
    op.create_table(
        "file_version_import_records",
        sa.Column(
            "import_id", sa.Uuid(), sa.ForeignKey("file_version_imports.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("byte_offset", sa.BigInteger(), nullable=False),
        sa.Column("payload_id", sa.Uuid(), sa.ForeignKey("record_payloads.id", ondelete="RESTRICT"), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_file_version_import_records_ordinal"),
        sa.CheckConstraint("byte_offset >= 0", name="ck_file_version_import_records_offset"),
    )


def downgrade() -> None:
    op.drop_table("file_version_import_records")
    op.drop_table("file_version_imports")

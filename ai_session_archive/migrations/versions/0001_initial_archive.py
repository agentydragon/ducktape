"""Initial versioned schema for the Restic-backed AI session archive."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_archive"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "machines",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "record_payloads",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=50), nullable=False),
        sa.Column("raw_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("parse_error", sa.Text()),
        sa.Column("timestamp", sa.DateTime(timezone=True)),
        sa.Column("kind", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256", "parser_version", name="uq_record_payloads_sha_parser"),
    )
    op.create_table(
        "usage_observations",
        sa.Column("payload_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("observation_index", sa.Integer(), nullable=False),
        sa.Column("source_field", sa.Text(), nullable=False),
        sa.Column("counter_kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text()),
        sa.Column("response_id", sa.Text()),
        sa.Column("message_id", sa.Text()),
        sa.Column("turn_id", sa.Text()),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("cache_read_input_tokens", sa.BigInteger()),
        sa.Column("cache_write_input_tokens", sa.BigInteger()),
        sa.Column("reasoning_output_tokens", sa.BigInteger()),
        sa.Column("total_tokens", sa.BigInteger()),
        sa.CheckConstraint("observation_index >= 0", name="ck_usage_observations_index"),
        sa.CheckConstraint(
            "counter_kind IN ('response', 'last_response', 'turn', 'thread', 'session', 'message', 'result', 'model_result')",
            name="ck_usage_observations_counter_kind",
        ),
        sa.ForeignKeyConstraint(["payload_id"], ["record_payloads.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("payload_id", "observation_index"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("machine_id", sa.String(length=100), nullable=False),
        sa.Column("harness", sa.String(length=20), nullable=False),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("source_relative_path", sa.String(), nullable=False),
        sa.Column("native_session_id", sa.String()),
        sa.Column("native_session_id_inferred", sa.Boolean(), nullable=False),
        sa.Column("native_session_id_conflict", sa.Boolean(), nullable=False),
        sa.Column("native_session_id_candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parent_session_id", sa.Uuid(as_uuid=True)),
        sa.Column("parent_native_session_id", sa.String()),
        sa.Column("parent_native_session_id_conflict", sa.Boolean(), nullable=False),
        sa.Column("parent_native_session_id_candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parent_link_kind", sa.String(length=40)),
        sa.Column("identity_inferred", sa.Boolean(), nullable=False),
        sa.Column("title", sa.String()),
        sa.Column("cwd", sa.String()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("harness IN ('claude', 'codex')", name="ck_sessions_harness"),
        sa.ForeignKeyConstraint(["machine_id"], ["machines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("machine_id", "harness", "source_path", name="uq_sessions_source_path"),
    )
    op.create_table(
        "restic_snapshots",
        sa.Column("machine_id", sa.String(length=100), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parent_snapshot_id", sa.String(length=64)),
        sa.Column("tree_id", sa.String(length=64)),
        sa.Column("paths", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("comparison_snapshot_id", sa.String(length=64)),
        sa.Column("inventory_status", sa.String(length=20), nullable=False),
        sa.Column("inventory_error", sa.String()),
        sa.Column("processing_status", sa.String(length=20), nullable=False),
        sa.Column("capture_status", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "capture_status IN ('unknown', 'complete', 'incomplete')", name="ck_restic_snapshots_capture_status"
        ),
        sa.CheckConstraint(
            "inventory_status IN ('pending', 'complete', 'failed')", name="ck_restic_snapshots_inventory_status"
        ),
        sa.CheckConstraint(
            "processing_status IN ('pending', 'complete', 'failed')", name="ck_restic_snapshots_processing_status"
        ),
        sa.ForeignKeyConstraint(["machine_id"], ["machines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("machine_id", "snapshot_id"),
    )
    op.create_table(
        "session_roots",
        sa.Column("machine_id", sa.String(length=100), nullable=False),
        sa.Column("harness", sa.String(length=20), nullable=False),
        sa.Column("root_path", sa.String(), nullable=False),
        sa.CheckConstraint("harness IN ('claude', 'codex')", name="ck_session_roots_harness"),
        sa.ForeignKeyConstraint(["machine_id"], ["machines.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("machine_id", "harness", "root_path"),
    )
    op.create_table(
        "file_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("parse_issue_count", sa.Integer(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(as_uuid=True)),
        sa.Column("shared_prefix_record_count", sa.Integer(), nullable=False),
        sa.Column("is_checkpoint", sa.Boolean(), nullable=False),
        sa.Column("delta_depth", sa.Integer(), nullable=False),
        sa.Column("parser_version", sa.String(length=50), nullable=False),
        sa.CheckConstraint("byte_size >= 0", name="ck_file_versions_byte_size"),
        sa.CheckConstraint("delta_depth >= 0", name="ck_file_versions_delta_depth"),
        sa.CheckConstraint("parse_issue_count >= 0", name="ck_file_versions_parse_issue_count"),
        sa.CheckConstraint("record_count >= 0", name="ck_file_versions_record_count"),
        sa.ForeignKeyConstraint(["parent_version_id"], ["file_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "sha256", name="uq_file_versions_session_sha256"),
    )
    op.create_table(
        "snapshot_files",
        sa.Column("machine_id", sa.String(length=100), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("session_id", sa.Uuid(as_uuid=True)),
        sa.Column("change_kind", sa.String(length=20), nullable=False),
        sa.Column("file_type", sa.String(length=30), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("file_version_id", sa.Uuid(as_uuid=True)),
        sa.Column("error", sa.String()),
        sa.CheckConstraint("byte_size >= 0", name="ck_snapshot_files_byte_size"),
        sa.CheckConstraint(
            "status IN ('pending', 'not_indexed', 'unclassified', 'deleted', 'complete', 'partial', 'failed')",
            name="ck_snapshot_files_status",
        ),
        sa.ForeignKeyConstraint(["file_version_id"], ["file_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["machine_id", "snapshot_id"],
            ["restic_snapshots.machine_id", "restic_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("machine_id", "snapshot_id", "path"),
    )
    op.create_table(
        "file_version_records",
        sa.Column("file_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("byte_offset", sa.BigInteger(), nullable=False),
        sa.Column("payload_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.CheckConstraint("byte_offset >= 0", name="ck_file_version_records_offset"),
        sa.CheckConstraint("ordinal >= 0", name="ck_file_version_records_ordinal"),
        sa.ForeignKeyConstraint(["file_version_id"], ["file_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payload_id"], ["record_payloads.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("file_version_id", "ordinal"),
    )
    op.create_index("idx_file_versions_session_seen", "file_versions", ["session_id", "first_seen_at", "id"])
    op.create_index("idx_file_version_records_payload", "file_version_records", ["payload_id"])
    op.create_index("idx_restic_snapshots_machine_time", "restic_snapshots", ["machine_id", "captured_at"])
    op.create_index(
        "idx_sessions_machine_harness_activity", "sessions", ["machine_id", "harness", "last_activity_at", "id"]
    )
    op.create_index(
        "idx_sessions_machine_harness_conflict", "sessions", ["machine_id", "harness", "native_session_id_conflict"]
    )
    op.create_index("idx_sessions_machine_harness_native", "sessions", ["machine_id", "harness", "native_session_id"])
    op.create_index("idx_snapshot_files_retry", "snapshot_files", ["status", "machine_id", "snapshot_id"])
    op.create_index("idx_snapshot_files_version", "snapshot_files", ["file_version_id"])


def downgrade() -> None:
    op.drop_index("idx_snapshot_files_version", table_name="snapshot_files")
    op.drop_index("idx_snapshot_files_retry", table_name="snapshot_files")
    op.drop_index("idx_sessions_machine_harness_native", table_name="sessions")
    op.drop_index("idx_sessions_machine_harness_conflict", table_name="sessions")
    op.drop_index("idx_sessions_machine_harness_activity", table_name="sessions")
    op.drop_index("idx_restic_snapshots_machine_time", table_name="restic_snapshots")
    op.drop_index("idx_file_version_records_payload", table_name="file_version_records")
    op.drop_index("idx_file_versions_session_seen", table_name="file_versions")
    op.drop_table("file_version_records")
    op.drop_table("snapshot_files")
    op.drop_table("file_versions")
    op.drop_table("session_roots")
    op.drop_table("restic_snapshots")
    op.drop_table("sessions")
    op.drop_table("usage_observations")
    op.drop_table("record_payloads")
    op.drop_table("machines")

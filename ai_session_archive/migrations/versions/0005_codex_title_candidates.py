"""Maintain compact Codex candidates while retaining the observation ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0005_codex_title_candidates"
down_revision = "0004_claude_title_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "codex_title_candidates",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("machine_id", sa.String(100), nullable=False),
        sa.Column("native_session_id", sa.String(), nullable=False),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("field", sa.String(20), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.CheckConstraint("field IN ('title', 'session_kind')", name="ck_codex_title_candidates_field"),
    )
    op.create_index(
        "idx_codex_title_candidates_machine_native", "codex_title_candidates", ["machine_id", "native_session_id"]
    )
    op.create_table(
        "codex_title_backfill",
        sa.Column("machine_id", sa.String(100), sa.ForeignKey("machines.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("snapshot_id", sa.String(64)),
        sa.Column("native_session_id", sa.String()),
        sa.Column("latest_snapshot_seeded", sa.Boolean(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
    )
    # This lock is also acquired by the bounded backfill. Hash collisions only
    # serialize unrelated machines; full paths are compared without a size-limited index.
    op.execute("""
        CREATE FUNCTION archive_lock_codex_title(machine text) RETURNS void
        LANGUAGE sql AS $$
            SELECT pg_advisory_xact_lock(hashtextextended(machine, 741327));
        $$
    """)
    op.execute("""
        CREATE FUNCTION archive_update_codex_title_candidate() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            candidate codex_title_candidates%ROWTYPE;
            field_name text;
            new_value text;
            observed_at timestamptz;
            replacement record;
        BEGIN
            IF TG_OP = 'UPDATE' AND
                (OLD.machine_id, OLD.snapshot_id, OLD.native_session_id, OLD.source_path, OLD.title, OLD.session_kind)
                IS NOT DISTINCT FROM
                (NEW.machine_id, NEW.snapshot_id, NEW.native_session_id, NEW.source_path, NEW.title, NEW.session_kind)
            THEN
                RETURN NEW;
            END IF;
            IF TG_OP <> 'INSERT' THEN
                PERFORM archive_lock_codex_title(OLD.machine_id);
                FOREACH field_name IN ARRAY ARRAY['title', 'session_kind'] LOOP
                    -- A retry can retract a winner, null one field, or move its path.
                    -- Recompute only that field's former mapping, using the native-ID index.
                    SELECT * INTO candidate FROM codex_title_candidates
                    WHERE machine_id = OLD.machine_id AND native_session_id = OLD.native_session_id
                        AND source_path = OLD.source_path AND field = field_name;
                    IF FOUND AND candidate.snapshot_id = OLD.snapshot_id AND (
                        TG_OP = 'DELETE' OR NEW.machine_id IS DISTINCT FROM OLD.machine_id
                        OR NEW.native_session_id IS DISTINCT FROM OLD.native_session_id
                        OR NEW.source_path IS DISTINCT FROM OLD.source_path
                        OR CASE field_name WHEN 'title' THEN NEW.title ELSE NEW.session_kind END IS NULL
                    ) THEN
                        SELECT o.snapshot_id, s.captured_at,
                            CASE field_name WHEN 'title' THEN o.title ELSE o.session_kind END AS value
                        INTO replacement
                        FROM codex_title_observations o
                        JOIN restic_snapshots s USING (machine_id, snapshot_id)
                        WHERE o.machine_id = OLD.machine_id AND o.native_session_id = OLD.native_session_id
                            AND o.source_path = OLD.source_path
                            AND CASE field_name WHEN 'title' THEN o.title ELSE o.session_kind END IS NOT NULL
                        ORDER BY s.captured_at DESC, o.snapshot_id DESC LIMIT 1;
                        IF FOUND THEN
                            UPDATE codex_title_candidates SET snapshot_id = replacement.snapshot_id,
                                captured_at = replacement.captured_at, value = replacement.value
                            WHERE id = candidate.id;
                        ELSE
                            DELETE FROM codex_title_candidates WHERE id = candidate.id;
                        END IF;
                    END IF;
                END LOOP;
            END IF;
            IF TG_OP <> 'DELETE' THEN
                PERFORM archive_lock_codex_title(NEW.machine_id);
                SELECT captured_at INTO STRICT observed_at FROM restic_snapshots
                WHERE machine_id = NEW.machine_id AND snapshot_id = NEW.snapshot_id;
                FOREACH field_name IN ARRAY ARRAY['title', 'session_kind'] LOOP
                    new_value := CASE field_name WHEN 'title' THEN NEW.title ELSE NEW.session_kind END;
                    IF new_value IS NULL THEN CONTINUE; END IF;
                    SELECT * INTO candidate FROM codex_title_candidates
                    WHERE machine_id = NEW.machine_id AND native_session_id = NEW.native_session_id
                        AND source_path = NEW.source_path AND field = field_name;
                    IF NOT FOUND THEN
                        INSERT INTO codex_title_candidates
                            (machine_id, native_session_id, source_path, field, value, captured_at, snapshot_id)
                        VALUES (NEW.machine_id, NEW.native_session_id, NEW.source_path, field_name,
                            new_value, observed_at, NEW.snapshot_id);
                    ELSIF (observed_at, NEW.snapshot_id) > (candidate.captured_at, candidate.snapshot_id)
                        OR ((observed_at, NEW.snapshot_id) = (candidate.captured_at, candidate.snapshot_id)
                            AND candidate.value IS DISTINCT FROM new_value) THEN
                        UPDATE codex_title_candidates SET value = new_value,
                            captured_at = observed_at, snapshot_id = NEW.snapshot_id
                        WHERE id = candidate.id;
                    END IF;
                END LOOP;
                RETURN NEW;
            END IF;
            RETURN OLD;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER codex_title_candidates_changed
        AFTER INSERT OR UPDATE OR DELETE ON codex_title_observations
        FOR EACH ROW EXECUTE FUNCTION archive_update_codex_title_candidate()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER codex_title_candidates_changed ON codex_title_observations")
    op.execute("DROP FUNCTION archive_update_codex_title_candidate()")
    op.execute("DROP FUNCTION archive_lock_codex_title(text)")
    op.drop_table("codex_title_backfill")
    op.drop_table("codex_title_candidates")

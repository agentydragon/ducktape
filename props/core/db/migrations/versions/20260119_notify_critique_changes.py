"""Add pg_notify triggers for critique changes and update notification format.

The grader daemon needs to wake up not only when ground truth changes,
but also when new critiques are reported that need grading.

Also updates the notification payload format to:
1. Use a nested event object for discriminated union support
2. Split type into separate 'table' and 'operation' fields
3. Include affected row IDs in the event payload

Revision ID: 20260119_notify_critique
Revises: 20260119_drop_events_table
Create Date: 2026-01-19
"""

from alembic import op

revision = "20260119_notify_critique"
down_revision = "20260119_drop_events_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Update existing GT trigger function to include IDs in payload
    # Each table has different columns, so we build the event object dynamically
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_gt_changed() RETURNS TRIGGER AS $$
        DECLARE
            v_row RECORD;
            v_event JSONB;
        BEGIN
            v_row := COALESCE(NEW, OLD);
            v_event := json_build_object(
                'table', TG_TABLE_NAME,
                'operation', TG_OP
            );

            -- Add table-specific IDs to event
            CASE TG_TABLE_NAME
                WHEN 'true_positives' THEN
                    v_event := v_event || json_build_object('tp_id', v_row.tp_id);
                WHEN 'true_positive_occurrences' THEN
                    v_event := v_event || json_build_object(
                        'tp_id', v_row.tp_id,
                        'occurrence_id', v_row.occurrence_id
                    );
                WHEN 'false_positives' THEN
                    v_event := v_event || json_build_object('fp_id', v_row.fp_id);
                WHEN 'false_positive_occurrences' THEN
                    v_event := v_event || json_build_object(
                        'fp_id', v_row.fp_id,
                        'occurrence_id', v_row.occurrence_id
                    );
            END CASE;

            PERFORM pg_notify('grading_pending', json_build_object(
                'snapshot_slug', v_row.snapshot_slug,
                'event', v_event
            )::text);
            RETURN v_row;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Create trigger function for critique changes
    # Unlike GT tables, reported_issues doesn't have snapshot_slug directly -
    # we need to look it up from the agent_run's type_config
    op.execute("""
        CREATE FUNCTION notify_critique_changed() RETURNS TRIGGER AS $$
        DECLARE
            v_snapshot_slug VARCHAR;
            v_event JSONB;
        BEGIN
            -- Look up snapshot_slug from the agent_run's type_config
            SELECT ar.type_config->'example'->>'snapshot_slug'
            INTO v_snapshot_slug
            FROM agent_runs ar
            WHERE ar.agent_run_id = NEW.agent_run_id;

            IF v_snapshot_slug IS NOT NULL THEN
                v_event := json_build_object(
                    'table', TG_TABLE_NAME,
                    'operation', TG_OP
                );

                -- Add table-specific IDs to event
                CASE TG_TABLE_NAME
                    WHEN 'reported_issues' THEN
                        v_event := v_event || json_build_object(
                            'agent_run_id', NEW.agent_run_id,
                            'issue_id', NEW.issue_id
                        );
                    WHEN 'reported_issue_occurrences' THEN
                        v_event := v_event || json_build_object(
                            'occurrence_id', NEW.id,
                            'agent_run_id', NEW.agent_run_id,
                            'reported_issue_id', NEW.reported_issue_id
                        );
                END CASE;

                PERFORM pg_notify('grading_pending', json_build_object(
                    'snapshot_slug', v_snapshot_slug,
                    'event', v_event
                )::text);
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        COMMENT ON FUNCTION notify_critique_changed() IS
        'Sends pg_notify when new critiques are reported. Used to wake snapshot_grader daemons.
Fires on INSERT of reported_issues and reported_issue_occurrences.
Looks up snapshot_slug from agent_run type_config since critique tables do not store it directly.'
    """)

    # Triggers on critique tables (INSERT only - we don't re-grade on updates or deletes)
    op.execute("""
        CREATE TRIGGER trg_notify_reported_issue_changed
        AFTER INSERT ON reported_issues
        FOR EACH ROW EXECUTE FUNCTION notify_critique_changed()
    """)

    op.execute("""
        CREATE TRIGGER trg_notify_reported_issue_occ_changed
        AFTER INSERT ON reported_issue_occurrences
        FOR EACH ROW EXECUTE FUNCTION notify_critique_changed()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_notify_reported_issue_occ_changed ON reported_issue_occurrences")
    op.execute("DROP TRIGGER IF EXISTS trg_notify_reported_issue_changed ON reported_issues")
    op.execute("DROP FUNCTION IF EXISTS notify_critique_changed()")

    # Restore original GT trigger function format
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_gt_changed() RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify('grading_pending', json_build_object(
                'event', TG_OP || '_' || TG_TABLE_NAME,
                'snapshot_slug', COALESCE(NEW.snapshot_slug, OLD.snapshot_slug)
            )::text);
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
    """)

"""Update pg_notify notification format.

Revision ID: 20260120_notification_format_update
Revises: 20260120_llm_requests_split_rls
Create Date: 2026-01-20

Changes notification format from:
  {snapshot_slug, event: {table, operation, ...keys}}
to:
  {operation, item: {table, ...keys}, snapshot_slug}
"""

from alembic import op

revision = "20260120_notification_format_update"
down_revision = "20260120_llm_requests_split_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Update GT notification trigger function
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_gt_changed() RETURNS TRIGGER AS $$
        DECLARE
            v_row RECORD;
            v_item JSONB;
        BEGIN
            v_row := COALESCE(NEW, OLD);

            -- Build item based on table
            v_item := json_build_object('table', TG_TABLE_NAME);

            CASE TG_TABLE_NAME
                WHEN 'true_positives' THEN
                    v_item := v_item || json_build_object('tp_id', v_row.tp_id);
                WHEN 'true_positive_occurrences' THEN
                    v_item := v_item || json_build_object('tp_id', v_row.tp_id, 'occurrence_id', v_row.occurrence_id);
                WHEN 'false_positives' THEN
                    v_item := v_item || json_build_object('fp_id', v_row.fp_id);
                WHEN 'false_positive_occurrences' THEN
                    v_item := v_item || json_build_object('fp_id', v_row.fp_id, 'occurrence_id', v_row.occurrence_id);
            END CASE;

            PERFORM pg_notify('grading_pending', json_build_object(
                'operation', TG_OP,
                'item', v_item,
                'snapshot_slug', v_row.snapshot_slug
            )::text);
            RETURN v_row;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Update critique notification trigger function
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_critique_changed() RETURNS TRIGGER AS $$
        DECLARE
            v_snapshot_slug VARCHAR;
            v_item JSONB;
        BEGIN
            -- Look up snapshot_slug from the agent_run's type_config
            SELECT ar.type_config->'example'->>'snapshot_slug'
            INTO v_snapshot_slug
            FROM agent_runs ar
            WHERE ar.agent_run_id = NEW.agent_run_id;

            IF v_snapshot_slug IS NOT NULL THEN
                v_item := json_build_object('table', TG_TABLE_NAME);

                -- Add table-specific IDs to item
                CASE TG_TABLE_NAME
                    WHEN 'reported_issues' THEN
                        v_item := v_item || json_build_object(
                            'agent_run_id', NEW.agent_run_id,
                            'issue_id', NEW.issue_id
                        );
                    WHEN 'reported_issue_occurrences' THEN
                        v_item := v_item || json_build_object(
                            'occurrence_id', NEW.id,
                            'agent_run_id', NEW.agent_run_id,
                            'reported_issue_id', NEW.reported_issue_id
                        );
                END CASE;

                PERFORM pg_notify('grading_pending', json_build_object(
                    'operation', TG_OP,
                    'item', v_item,
                    'snapshot_slug', v_snapshot_slug
                )::text);
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)


def downgrade() -> None:
    # Restore original GT trigger function
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

            CASE TG_TABLE_NAME
                WHEN 'true_positives' THEN
                    v_event := v_event || json_build_object('tp_id', v_row.tp_id);
                WHEN 'true_positive_occurrences' THEN
                    v_event := v_event || json_build_object('tp_id', v_row.tp_id, 'occurrence_id', v_row.occurrence_id);
                WHEN 'false_positives' THEN
                    v_event := v_event || json_build_object('fp_id', v_row.fp_id);
                WHEN 'false_positive_occurrences' THEN
                    v_event := v_event || json_build_object('fp_id', v_row.fp_id, 'occurrence_id', v_row.occurrence_id);
            END CASE;

            PERFORM pg_notify('grading_pending', json_build_object(
                'snapshot_slug', v_row.snapshot_slug,
                'event', v_event
            )::text);
            RETURN v_row;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Restore original critique trigger function
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_critique_changed() RETURNS TRIGGER AS $$
        DECLARE
            v_snapshot_slug VARCHAR;
            v_event JSONB;
        BEGIN
            SELECT ar.type_config->'example'->>'snapshot_slug'
            INTO v_snapshot_slug
            FROM agent_runs ar
            WHERE ar.agent_run_id = NEW.agent_run_id;

            IF v_snapshot_slug IS NOT NULL THEN
                v_event := json_build_object(
                    'table', TG_TABLE_NAME,
                    'operation', TG_OP
                );

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

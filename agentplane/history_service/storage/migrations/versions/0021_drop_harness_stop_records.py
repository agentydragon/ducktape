"""Delete the retained harness-stop commands from stored history and the fold.

No RPC accepts a stop command and no runner executes one. For every folded stop command with an
outcome this deletes its admission entry, and its failed or no-op outcome entry, from
`session_event`; records the deleted cursors as removed ranges; and removes the command's
projection, renumbering the Session's entities densely. An effected stop's outcome is the retained
`HarnessExited` entry, which keeps its other fields, and the fold's copy of it loses
`stoppedByCommandId`.

A stop with no outcome stays: its runner still journals it as pending and will settle it, and that
outcome entry needs the admission. A stop admitted after the fold's checkpoint is not found here
either. Both wait for a later cleanup.

This deletes History entries; take a CNPG `Backup` first.
"""

from alembic import op

revision = "0021_drop_harness_stop_records"
down_revision = "0020_removed_ranges"
branch_labels = None
depends_on = None

# The oneof field's name when the admission was folded, and its current one.
_STOP_OPERATIONS = "('stop_runner_session', 'historical_harness_stop')"


def upgrade() -> None:
    op.execute(f"""
        CREATE TEMPORARY TABLE harness_stop_command ON COMMIT DROP AS
        SELECT thread_id, projection_epoch, entity_id AS command_id, cursor AS admission_cursor,
            CASE WHEN state->>'outcome' IN ('failed', 'noop') THEN (state->>'outcome_cursor')::bigint END
                AS outcome_cursor
        FROM thread_entity
        WHERE entity_kind = 'command' AND state->>'operation' IN {_STOP_OPERATIONS}
            AND state->>'outcome' <> 'pending'
    """)
    # Only cursors that are still stored: a range never covers a retained entry or a settled delta.
    op.execute("""
        CREATE TEMPORARY TABLE removed_cursor ON COMMIT DROP AS
        SELECT DISTINCT stored.session_id, stored.cursor
        FROM session_event AS stored
        JOIN harness_stop_command AS stop ON stop.thread_id = stored.session_id
            AND stored.cursor IN (stop.admission_cursor, stop.outcome_cursor)
    """)
    op.execute("""
        INSERT INTO session_event_removed_range (session_id, first_cursor, last_cursor)
        SELECT session_id, min(cursor), max(cursor)
        FROM (
            SELECT session_id, cursor,
                cursor - row_number() OVER (PARTITION BY session_id ORDER BY cursor) AS run
            FROM removed_cursor
        ) AS numbered
        GROUP BY session_id, run
    """)
    op.execute("""
        DELETE FROM session_event AS stored USING removed_cursor AS removed
        WHERE stored.session_id = removed.session_id AND stored.cursor = removed.cursor
    """)
    for table in ("thread_native_link", "thread_evidence"):
        op.execute(f"""
            DELETE FROM {table} AS evidence USING harness_stop_command AS stop
            WHERE evidence.thread_id = stop.thread_id AND evidence.projection_epoch = stop.projection_epoch
                AND evidence.entity_cursor = stop.admission_cursor
        """)
    op.execute("""
        DELETE FROM thread_entity AS entity USING harness_stop_command AS stop
        WHERE entity.thread_id = stop.thread_id AND entity.projection_epoch = stop.projection_epoch
            AND entity.entity_kind = 'command' AND entity.entity_id = stop.command_id
    """)
    op.execute("""
        UPDATE thread_entity
        SET state = jsonb_set(state, '{event,harnessExited}', (state #> '{event,harnessExited}') - 'stoppedByCommandId')
        WHERE entity_kind = 'lifecycle' AND state #> '{event,harnessExited,stoppedByCommandId}' IS NOT NULL
    """)
    # The unique index is checked row by row, so a shift down would collide: park the indexes below zero first.
    scope = "SELECT DISTINCT thread_id, projection_epoch FROM harness_stop_command"
    op.execute(f"""
        UPDATE thread_entity AS entity SET entity_index = -entity_index - 1
        FROM ({scope}) AS affected
        WHERE entity.thread_id = affected.thread_id AND entity.projection_epoch = affected.projection_epoch
    """)
    op.execute(f"""
        UPDATE thread_entity AS entity SET entity_index = renumbered.entity_index
        FROM (
            SELECT thread_id, projection_epoch, entity_kind, entity_id,
                row_number() OVER (PARTITION BY thread_id, projection_epoch ORDER BY entity_index DESC) - 1
                    AS entity_index
            FROM thread_entity
            WHERE (thread_id, projection_epoch) IN ({scope})
        ) AS renumbered
        WHERE entity.thread_id = renumbered.thread_id AND entity.projection_epoch = renumbered.projection_epoch
            AND entity.entity_kind = renumbered.entity_kind AND entity.entity_id = renumbered.entity_id
    """)


def downgrade() -> None:
    raise RuntimeError("the deleted harness-stop records are gone; restore the CNPG backup")

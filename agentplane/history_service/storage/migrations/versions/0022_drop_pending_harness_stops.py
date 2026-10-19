"""Delete the harness-stop commands that `0021_drop_harness_stop_records` left with their outcome pending.

Removes exactly what 0021 removes, for every folded stop command whatever its outcome. A stop pending
at the end of a runner's journal is settled only by that runner, and the outcome entry it would then
write cannot be matched to a deleted admission, so run this only where no runner can still settle
one: the one stop staging kept belongs to a Sandbox that no longer exists.

This deletes History entries; take a CNPG `Backup` first.
"""

from alembic import op

revision = "0022_drop_pending_harness_stops"
down_revision = "0021_drop_harness_stop_records"
branch_labels = None
depends_on = None

# The oneof field's name when the admission was folded, and its current one.
# The temporary tables are named apart from 0021's: both run in one transaction, which keeps them until commit.
_STOP_OPERATIONS = "('stop_runner_session', 'historical_harness_stop')"


def upgrade() -> None:
    op.execute(f"""
        CREATE TEMPORARY TABLE leftover_stop_command ON COMMIT DROP AS
        SELECT thread_id, projection_epoch, entity_id AS command_id, cursor AS admission_cursor,
            CASE WHEN state->>'outcome' IN ('failed', 'noop') THEN (state->>'outcome_cursor')::bigint END
                AS outcome_cursor
        FROM thread_entity
        WHERE entity_kind = 'command' AND state->>'operation' IN {_STOP_OPERATIONS}
    """)
    # Only cursors that are still stored: a range never covers a retained entry or a settled delta.
    op.execute("""
        CREATE TEMPORARY TABLE leftover_removed_cursor ON COMMIT DROP AS
        SELECT DISTINCT stored.session_id, stored.cursor
        FROM session_event AS stored
        JOIN leftover_stop_command AS stop ON stop.thread_id = stored.session_id
            AND stored.cursor IN (stop.admission_cursor, stop.outcome_cursor)
    """)
    op.execute("""
        INSERT INTO session_event_removed_range (session_id, first_cursor, last_cursor)
        SELECT session_id, min(cursor), max(cursor)
        FROM (
            SELECT session_id, cursor,
                cursor - row_number() OVER (PARTITION BY session_id ORDER BY cursor) AS run
            FROM leftover_removed_cursor
        ) AS numbered
        GROUP BY session_id, run
    """)
    op.execute("""
        DELETE FROM session_event AS stored USING leftover_removed_cursor AS removed
        WHERE stored.session_id = removed.session_id AND stored.cursor = removed.cursor
    """)
    for table in ("thread_native_link", "thread_evidence"):
        op.execute(f"""
            DELETE FROM {table} AS evidence USING leftover_stop_command AS stop
            WHERE evidence.thread_id = stop.thread_id AND evidence.projection_epoch = stop.projection_epoch
                AND evidence.entity_cursor = stop.admission_cursor
        """)
    op.execute("""
        DELETE FROM thread_entity AS entity USING leftover_stop_command AS stop
        WHERE entity.thread_id = stop.thread_id AND entity.projection_epoch = stop.projection_epoch
            AND entity.entity_kind = 'command' AND entity.entity_id = stop.command_id
    """)
    op.execute("""
        UPDATE thread_entity
        SET state = jsonb_set(state, '{event,harnessExited}', (state #> '{event,harnessExited}') - 'stoppedByCommandId')
        WHERE entity_kind = 'lifecycle' AND state #> '{event,harnessExited,stoppedByCommandId}' IS NOT NULL
    """)
    # The unique index is checked row by row, so a shift down would collide: park the indexes below zero first.
    scope = "SELECT DISTINCT thread_id, projection_epoch FROM leftover_stop_command"
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

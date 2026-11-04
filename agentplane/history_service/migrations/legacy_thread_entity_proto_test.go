package migrations

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"
)

const threadEntityBatchSize = 128

// migrateThreadEntities runs in Goose's transaction. The lock fences all old writers before
// conversion; all state updates, dropped columns, and the version record commit together.
// Failed/cancelled startups leave every old row and column intact. Keyset batches bound memory
// without retaining the complete history or changing the primary-key order while scanning.
func migrateThreadEntities(ctx context.Context, tx *sql.Tx) error {
	if _, err := tx.ExecContext(ctx, "LOCK TABLE thread_entity IN ACCESS EXCLUSIVE MODE"); err != nil {
		return err
	}
	var after *threadEntityV6
	for {
		query := `SELECT thread_id, projection_epoch, entity_kind, entity_id, cursor, revision_cursor, pending, turn_id, state, text_ref, arguments_ref, output_ref, input_ref FROM thread_entity`
		args := []any{}
		if after != nil {
			query += ` WHERE (thread_id,projection_epoch,entity_kind,entity_id)>($1::uuid,$2,$3,$4)`
			args = []any{after.ThreadID, after.ProjectionEpoch, after.EntityKind, after.EntityID}
		}
		query += fmt.Sprintf(" ORDER BY thread_id,projection_epoch,entity_kind,entity_id LIMIT %d", threadEntityBatchSize)
		rows, err := tx.QueryContext(ctx, query, args...)
		if err != nil {
			return err
		}
		batch := make([]threadEntityV6, 0, threadEntityBatchSize)
		for rows.Next() {
			var row threadEntityV6
			var textRef, argumentsRef, outputRef, inputRef []byte
			if err := rows.Scan(&row.ThreadID, &row.ProjectionEpoch, &row.EntityKind, &row.EntityID, &row.Cursor, &row.RevisionCursor, &row.Pending, &row.TurnID, &row.State, &textRef, &argumentsRef, &outputRef, &inputRef); err != nil {
				_ = rows.Close()
				return err
			}
			row.TextRef, row.ArgumentsRef, row.OutputRef, row.InputRef = textRef, argumentsRef, outputRef, inputRef
			batch = append(batch, row)
		}
		if err := errors.Join(rows.Err(), rows.Close()); err != nil {
			return err
		}
		if len(batch) == 0 {
			break
		}
		values := make([]string, 0, len(batch))
		updateArgs := make([]any, 0, len(batch)*5)
		for _, row := range batch {
			value, err := row.canonicalState()
			if err != nil {
				return fmt.Errorf("migrate thread_entity %s/%s/%s/%s: %w", row.ThreadID, row.ProjectionEpoch, row.EntityKind, row.EntityID, err)
			}
			state, err := protojson.Marshal(value)
			if err != nil {
				return err
			}
			first := len(updateArgs) + 1
			values = append(values, fmt.Sprintf("($%d::uuid,$%d::text,$%d::text,$%d::text,$%d::jsonb)", first, first+1, first+2, first+3, first+4))
			updateArgs = append(updateArgs, row.ThreadID, row.ProjectionEpoch, row.EntityKind, row.EntityID, string(state))
		}
		result, err := tx.ExecContext(ctx, `UPDATE thread_entity AS entity SET state=converted.state FROM (VALUES `+strings.Join(values, ",")+`) AS converted(thread_id,projection_epoch,entity_kind,entity_id,state) WHERE entity.thread_id=converted.thread_id AND entity.projection_epoch=converted.projection_epoch AND entity.entity_kind=converted.entity_kind AND entity.entity_id=converted.entity_id`, updateArgs...)
		if err != nil {
			return err
		}
		if updated, err := result.RowsAffected(); err != nil {
			return err
		} else if updated != int64(len(batch)) {
			return fmt.Errorf("converted %d of %d thread entities", updated, len(batch))
		}
		after = &batch[len(batch)-1]
	}
	_, err := tx.ExecContext(ctx, `ALTER TABLE thread_entity DROP COLUMN turn_id, DROP COLUMN text_ref, DROP COLUMN arguments_ref, DROP COLUMN output_ref, DROP COLUMN input_ref`)
	return err
}

func rollbackThreadEntities(context.Context, *sql.Tx) error {
	return errors.New("History v7 changes the stored Thread representation; restore a pre-cutover database backup before running pre-v7 binaries")
}

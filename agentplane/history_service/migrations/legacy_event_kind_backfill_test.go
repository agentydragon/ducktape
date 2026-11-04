package migrations

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

const eventKindBatchSize = 512

type eventKindCursor struct {
	sessionID string
	cursor    int64
}

// backfillEventKinds walks session_event in primary-key order, one transaction per bounded batch, so an
// interrupted migration resumes without retaining a transaction over the whole history table. Selecting
// on `kind IS NULL` instead lets the planner scan the kind index, whose NULL entries still include every
// row already rewritten until vacuum removes them: each batch then rereads the whole backfilled prefix.
func backfillEventKinds(ctx context.Context, db *sql.DB) error {
	// Cursors are non-negative, so this key precedes every row.
	after := eventKindCursor{sessionID: "00000000-0000-0000-0000-000000000000", cursor: -1}
	for {
		next, err := backfillEventKindBatch(ctx, db, after)
		if err != nil {
			return err
		}
		if next == nil {
			break
		}
		after = *next
	}
	return requireEventKinds(ctx, db)
}

func backfillEventKindBatch(ctx context.Context, db *sql.DB, after eventKindCursor) (*eventKindCursor, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	// Selecting `session_id::text` would name that output column `session_id`, so ORDER BY would sort the text
	// and Postgres would sort the whole table for every batch instead of reading the primary key.
	rows, err := tx.QueryContext(ctx, `SELECT session_id, cursor, kind IS NULL, CASE WHEN kind IS NULL THEN payload END
		FROM session_event WHERE (session_id, cursor) > ($1::uuid, $2)
		ORDER BY session_id, cursor LIMIT $3 FOR UPDATE`, after.sessionID, after.cursor, eventKindBatchSize)
	if err != nil {
		return nil, err
	}
	var last *eventKindCursor
	var sessionIDs []string
	var cursors, kinds []int64
	for rows.Next() {
		var row eventKindCursor
		var missing bool
		var payload []byte
		if err := rows.Scan(&row.sessionID, &row.cursor, &missing, &payload); err != nil {
			_ = rows.Close()
			return nil, err
		}
		last = &row
		if !missing {
			continue
		}
		kind, err := eventObservationNumber(payload)
		if err != nil {
			_ = rows.Close()
			return nil, fmt.Errorf("decode session_event %s cursor %d: %w", row.sessionID, row.cursor, err)
		}
		sessionIDs = append(sessionIDs, row.sessionID)
		cursors = append(cursors, row.cursor)
		kinds = append(kinds, int64(kind))
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return nil, err
	}
	if len(sessionIDs) > 0 {
		result, err := tx.ExecContext(ctx, `UPDATE session_event AS event SET kind = batch.kind
			FROM unnest($1::uuid[], $2::bigint[], $3::integer[]) AS batch(session_id, cursor, kind)
			WHERE event.session_id = batch.session_id AND event.cursor = batch.cursor AND event.kind IS NULL`,
			pq.Array(sessionIDs), pq.Array(cursors), pq.Array(kinds))
		if err != nil {
			return nil, err
		}
		updated, err := result.RowsAffected()
		if err != nil {
			return nil, err
		}
		if updated != int64(len(sessionIDs)) {
			return nil, fmt.Errorf("backfill session_event batch after %s cursor %d updated %d of %d rows",
				after.sessionID, after.cursor, updated, len(sessionIDs))
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return last, nil
}

// requireEventKinds makes kind NOT NULL without scanning the table under SET NOT NULL's ACCESS EXCLUSIVE
// lock: VALIDATE CONSTRAINT scans while reads and writes continue, and SET NOT NULL then trusts the valid
// check. Dropping a leftover check first lets an interrupted run start over.
func requireEventKinds(ctx context.Context, db *sql.DB) error {
	for _, statement := range []string{
		"ALTER TABLE session_event DROP CONSTRAINT IF EXISTS session_event_kind_present",
		"ALTER TABLE session_event ADD CONSTRAINT session_event_kind_present CHECK (kind IS NOT NULL) NOT VALID",
		"ALTER TABLE session_event VALIDATE CONSTRAINT session_event_kind_present",
		"ALTER TABLE session_event ALTER COLUMN kind SET NOT NULL",
		"ALTER TABLE session_event DROP CONSTRAINT session_event_kind_present",
	} {
		if _, err := db.ExecContext(ctx, statement); err != nil {
			return err
		}
	}
	return nil
}

func eventObservationNumber(payload []byte) (int32, error) {
	var entry eventlogpb.EventEntry
	if err := proto.Unmarshal(payload, &entry); err != nil {
		return 0, err
	}
	if entry.GetEvent() == nil {
		return 0, nil
	}
	event := entry.GetEvent().ProtoReflect()
	field := event.WhichOneof(event.Descriptor().Oneofs().ByName("observation"))
	if field == nil {
		return 0, nil
	}
	return int32(field.Number()), nil
}

func rollbackEventKindBackfill(ctx context.Context, db *sql.DB) error {
	_, err := db.ExecContext(ctx, "ALTER TABLE session_event ALTER COLUMN kind DROP NOT NULL")
	return err
}

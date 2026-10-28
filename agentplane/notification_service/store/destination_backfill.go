package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	model "github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

const (
	destinationBackfillBatchSize   = 512
	destinationBackfillBatchBytes  = 1 << 20
	destinationBackfillMaxRowBytes = 256 << 10
	destinationBackfillBatchLimit  = 30 * time.Second
)

const inboxDestinationCandidatesAfterCursor = `SELECT id, CASE
		WHEN destination_namespace IS NULL AND destination_name IS NULL AND destination_uid IS NULL
		THEN octet_length(destination_ref::text) ELSE 0 END
	FROM inbox WHERE id > $1::uuid ORDER BY id LIMIT $2 FOR UPDATE`

const inboxDestinationCandidatesFromStart = `SELECT id, CASE
		WHEN destination_namespace IS NULL AND destination_name IS NULL AND destination_uid IS NULL
		THEN octet_length(destination_ref::text) ELSE 0 END
	FROM inbox ORDER BY id LIMIT $1 FOR UPDATE`

const inboxDestinationRowsByID = `SELECT id, CASE
		WHEN destination_namespace IS NULL AND destination_name IS NULL AND destination_uid IS NULL
		THEN destination_ref END,
		destination_namespace, destination_name, destination_uid
	FROM inbox WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE`

const updateInboxDestinationsFromBatch = `UPDATE inbox AS inbox SET
	destination_namespace = batch.namespace, destination_name = batch.name, destination_uid = batch.uid
	FROM unnest($1::uuid[], $2::text[], $3::text[], $4::text[])
		AS batch(id, namespace, name, uid)
	WHERE inbox.id = batch.id AND inbox.destination_namespace IS NULL
		AND inbox.destination_name IS NULL AND inbox.destination_uid IS NULL`

type destinationBackfillCandidate struct {
	id         uuid.UUID
	serialized int64
}

type inboxDestinationBackfillRow struct {
	id          uuid.UUID
	destination []byte
	namespace   sql.NullString
	name        sql.NullString
	uid         sql.NullString
}

// BackfillInboxDestinations copies retained destination_ref values into their relational columns. Run it only
// after every Notification Service writer has deployed dual-write support. Each batch commits its row changes
// and UUID keyset cursor together, so an interrupted invocation resumes without rescanning a NULL prefix.
func BackfillInboxDestinations(ctx context.Context, db *sql.DB) error {
	reset := false
	for {
		batchCtx, cancel := context.WithTimeout(ctx, destinationBackfillBatchLimit)
		done, err := backfillInboxDestinations(batchCtx, db)
		cancel()
		if err != nil {
			return err
		}
		if done {
			validationCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)
			incomplete, err := inboxDestinationsIncomplete(validationCtx, db)
			cancel()
			if err != nil {
				return err
			}
			if !incomplete {
				return nil
			}
			if reset {
				return errors.New("inbox destination backfill found incomplete rows after a full rescan; verify all writers use notification-destination-dual-write-v1 and inspect partial destination columns")
			}
			resetCtx, cancel := context.WithTimeout(ctx, destinationBackfillBatchLimit)
			err = resetInboxDestinationBackfill(resetCtx, db)
			cancel()
			if err != nil {
				return err
			}
			reset = true
		}
	}
}

// A completed cursor can be stale if an old writer inserted a row behind it. Validate once at command
// completion, and restart from the beginning if such rows exist. This is deliberately not done per batch.
func inboxDestinationsIncomplete(ctx context.Context, db *sql.DB) (bool, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `SET LOCAL lock_timeout = '5s'`); err != nil {
		return false, err
	}
	if _, err := tx.ExecContext(ctx, `SET LOCAL statement_timeout = '5min'`); err != nil {
		return false, err
	}
	var incomplete bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM inbox WHERE destination_namespace IS NULL OR destination_name IS NULL OR destination_uid IS NULL
	)`).Scan(&incomplete); err != nil {
		return false, err
	}
	return incomplete, tx.Commit()
}

func resetInboxDestinationBackfill(ctx context.Context, db *sql.DB) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `SET LOCAL lock_timeout = '5s'`); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `SET LOCAL statement_timeout = '30s'`); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE notification_storage_backfill_progress
		SET inbox_destination_last_id = NULL, inbox_destination_complete = false WHERE singleton`); err != nil {
		return err
	}
	return tx.Commit()
}

func backfillInboxDestinations(ctx context.Context, db *sql.DB) (bool, error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `SET LOCAL lock_timeout = '5s'`); err != nil {
		return false, err
	}
	if _, err := tx.ExecContext(ctx, `SET LOCAL statement_timeout = '30s'`); err != nil {
		return false, err
	}

	var cursor sql.NullString
	var complete bool
	if err := tx.QueryRowContext(ctx, `SELECT inbox_destination_last_id::text, inbox_destination_complete
		FROM notification_storage_backfill_progress WHERE singleton FOR UPDATE`).Scan(&cursor, &complete); err != nil {
		return false, err
	}
	if complete {
		return true, tx.Commit()
	}

	candidates, err := inboxDestinationBackfillCandidates(ctx, tx, cursor)
	if err != nil {
		return false, err
	}
	if len(candidates) == 0 {
		if _, err := tx.ExecContext(ctx, `UPDATE notification_storage_backfill_progress
			SET inbox_destination_complete = true WHERE singleton`); err != nil {
			return false, err
		}
		return true, tx.Commit()
	}
	selected, _, err := boundedDestinationCandidates(candidates)
	if err != nil {
		return false, err
	}
	ids := make([]string, len(selected))
	for i, candidate := range selected {
		ids[i] = candidate.id.String()
	}

	rows, err := tx.QueryContext(ctx, inboxDestinationRowsByID, pq.Array(ids))
	if err != nil {
		return false, err
	}
	var batch []inboxDestinationBackfillRow
	for rows.Next() {
		var row inboxDestinationBackfillRow
		if err := rows.Scan(&row.id, &row.destination, &row.namespace, &row.name, &row.uid); err != nil {
			_ = rows.Close()
			return false, err
		}
		batch = append(batch, row)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return false, err
	}
	if len(batch) != len(selected) {
		return false, fmt.Errorf("inbox destination backfill selected %d rows but read %d", len(selected), len(batch))
	}

	var updateIDs, namespaces, names, uids []string
	for _, row := range batch {
		if row.namespace.Valid || row.name.Valid || row.uid.Valid {
			if !row.namespace.Valid || !row.name.Valid || !row.uid.Valid {
				return false, fmt.Errorf("inbox %s has incomplete normalized destination fields", row.id)
			}
			continue
		}
		ref, err := decodeDestinationForBackfill(row.id, row.destination)
		if err != nil {
			return false, err
		}
		updateIDs = append(updateIDs, row.id.String())
		namespaces = append(namespaces, ref.Namespace)
		names = append(names, ref.Name)
		uids = append(uids, ref.UID)
	}
	if len(updateIDs) > 0 {
		result, err := tx.ExecContext(ctx, updateInboxDestinationsFromBatch,
			pq.Array(updateIDs), pq.Array(namespaces), pq.Array(names), pq.Array(uids))
		if err != nil {
			return false, err
		}
		updated, err := result.RowsAffected()
		if err != nil {
			return false, err
		}
		if updated != int64(len(updateIDs)) {
			return false, fmt.Errorf("inbox destination backfill updated %d of %d rows", updated, len(updateIDs))
		}
	}

	done := len(selected) == len(candidates) && len(candidates) < destinationBackfillBatchSize
	if _, err := tx.ExecContext(ctx, `UPDATE notification_storage_backfill_progress
		SET inbox_destination_last_id = $1::uuid, inbox_destination_complete = $2 WHERE singleton`,
		selected[len(selected)-1].id.String(), done); err != nil {
		return false, err
	}
	return done, tx.Commit()
}

func inboxDestinationBackfillCandidates(ctx context.Context, tx *sql.Tx, cursor sql.NullString) ([]destinationBackfillCandidate, error) {
	query := inboxDestinationCandidatesFromStart
	args := []any{destinationBackfillBatchSize}
	if cursor.Valid {
		query = inboxDestinationCandidatesAfterCursor
		args = []any{cursor.String, destinationBackfillBatchSize}
	}
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var candidates []destinationBackfillCandidate
	for rows.Next() {
		var candidate destinationBackfillCandidate
		if err := rows.Scan(&candidate.id, &candidate.serialized); err != nil {
			return nil, err
		}
		candidates = append(candidates, candidate)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return candidates, nil
}

func boundedDestinationCandidates(candidates []destinationBackfillCandidate) ([]destinationBackfillCandidate, int64, error) {
	var selected []destinationBackfillCandidate
	var total int64
	for _, candidate := range candidates {
		if candidate.serialized > destinationBackfillMaxRowBytes {
			if len(selected) == 0 {
				return nil, total, fmt.Errorf("inbox %s destination_ref is %d bytes; maximum backfill row size is %d",
					candidate.id, candidate.serialized, destinationBackfillMaxRowBytes)
			}
			break
		}
		if len(selected) > 0 && total+candidate.serialized > destinationBackfillBatchBytes {
			break
		}
		selected = append(selected, candidate)
		total += candidate.serialized
	}
	return selected, total, nil
}

func decodeDestinationForBackfill(id uuid.UUID, data []byte) (model.DestinationRef, error) {
	var ref model.DestinationRef
	if err := decode(data, &ref); err != nil {
		return model.DestinationRef{}, fmt.Errorf("inbox %s destination_ref: %w", id, err)
	}
	if err := ref.Validate(); err != nil {
		return model.DestinationRef{}, fmt.Errorf("inbox %s destination_ref: %w", id, err)
	}
	return ref, nil
}

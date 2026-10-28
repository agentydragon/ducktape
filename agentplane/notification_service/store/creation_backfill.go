package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

const (
	subscriptionCreationBackfillBatchSize  = 512
	subscriptionCreationBackfillBatchBytes = 1 << 20
	subscriptionCreationBackfillBatchLimit = 30 * time.Second
)

const subscriptionCreationCandidatesAfterCursor = `SELECT id, CASE
		WHEN creation_proto IS NULL AND source_provider IS NULL THEN octet_length(creation::text) ELSE 0 END
	FROM subscription WHERE id > $1::uuid ORDER BY id LIMIT $2 FOR UPDATE`

const subscriptionCreationCandidatesFromStart = `SELECT id, CASE
		WHEN creation_proto IS NULL AND source_provider IS NULL THEN octet_length(creation::text) ELSE 0 END
	FROM subscription ORDER BY id LIMIT $1 FOR UPDATE`

const subscriptionCreationRowsByID = `SELECT id,
		CASE WHEN creation_proto IS NULL AND source_provider IS NULL THEN creation END,
		creation_proto IS NOT NULL, source_provider, idempotency_key
	FROM subscription WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE`

const updateSubscriptionCreationsFromBatch = `UPDATE subscription AS subscription SET
	creation_proto = batch.creation_proto, source_provider = batch.source_provider
	FROM unnest($1::uuid[], $2::text[], $3::bytea[]) AS batch(id, source_provider, creation_proto)
	WHERE subscription.id = batch.id AND subscription.creation_proto IS NULL AND subscription.source_provider IS NULL`

type subscriptionCreationBackfillCandidate struct {
	id         uuid.UUID
	serialized int64
}

type subscriptionCreationBackfillRow struct {
	id                   uuid.UUID
	legacy               []byte
	creationProtoPresent bool
	provider             sql.NullString
	idempotency          string
}

// BackfillSubscriptionCreations copies retained creation JSON into binary typed snapshots. Run only after every
// Notification Service writer has deployed dual-write support. Each batch commits its rows and UUID keyset cursor
// together, so interruption resumes without rescanning the completed prefix.
func BackfillSubscriptionCreations(ctx context.Context, db *sql.DB) error {
	reset := false
	for {
		batchCtx, cancel := context.WithTimeout(ctx, subscriptionCreationBackfillBatchLimit)
		done, err := backfillSubscriptionCreations(batchCtx, db)
		cancel()
		if err != nil {
			return err
		}
		if !done {
			continue
		}

		validationCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)
		incomplete, err := subscriptionCreationsIncomplete(validationCtx, db)
		cancel()
		if err != nil {
			return err
		}
		if !incomplete {
			return nil
		}
		if reset {
			return errors.New("subscription creation backfill found incomplete rows after a full rescan; verify all writers use notification-subscription-creation-dual-write-v1 and inspect partial typed fields")
		}
		resetCtx, cancel := context.WithTimeout(ctx, subscriptionCreationBackfillBatchLimit)
		err = resetSubscriptionCreationBackfill(resetCtx, db)
		cancel()
		if err != nil {
			return err
		}
		reset = true
	}
}

// A completed cursor can be stale if a legacy writer inserted a row below it. Validate once at command completion,
// and restart from the beginning if one remains. This full-table existence scan is never done per batch.
func subscriptionCreationsIncomplete(ctx context.Context, db *sql.DB) (bool, error) {
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
		SELECT 1 FROM subscription WHERE creation_proto IS NULL OR source_provider IS NULL
	)`).Scan(&incomplete); err != nil {
		return false, err
	}
	return incomplete, tx.Commit()
}

func resetSubscriptionCreationBackfill(ctx context.Context, db *sql.DB) error {
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
		SET subscription_creation_last_id = NULL, subscription_creation_complete = false WHERE singleton`); err != nil {
		return err
	}
	return tx.Commit()
}

func backfillSubscriptionCreations(ctx context.Context, db *sql.DB) (bool, error) {
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
	if err := tx.QueryRowContext(ctx, `SELECT subscription_creation_last_id::text, subscription_creation_complete
		FROM notification_storage_backfill_progress WHERE singleton FOR UPDATE`).Scan(&cursor, &complete); err != nil {
		return false, err
	}
	if complete {
		return true, tx.Commit()
	}

	candidates, err := subscriptionCreationBackfillCandidates(ctx, tx, cursor)
	if err != nil {
		return false, err
	}
	if len(candidates) == 0 {
		if _, err := tx.ExecContext(ctx, `UPDATE notification_storage_backfill_progress
			SET subscription_creation_complete = true WHERE singleton`); err != nil {
			return false, err
		}
		return true, tx.Commit()
	}
	selected, _, err := boundedSubscriptionCreationCandidates(candidates)
	if err != nil {
		return false, err
	}
	ids := make([]string, len(selected))
	for i, candidate := range selected {
		ids[i] = candidate.id.String()
	}

	rows, err := tx.QueryContext(ctx, subscriptionCreationRowsByID, pq.Array(ids))
	if err != nil {
		return false, err
	}
	var batch []subscriptionCreationBackfillRow
	for rows.Next() {
		var row subscriptionCreationBackfillRow
		if err := rows.Scan(&row.id, &row.legacy, &row.creationProtoPresent, &row.provider, &row.idempotency); err != nil {
			_ = rows.Close()
			return false, err
		}
		batch = append(batch, row)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return false, err
	}
	if len(batch) != len(selected) {
		return false, fmt.Errorf("subscription creation backfill selected %d rows but read %d", len(selected), len(batch))
	}

	var updateIDs, providers []string
	var encodedCreations [][]byte
	for _, row := range batch {
		if row.creationProtoPresent || row.provider.Valid {
			if !row.creationProtoPresent || !row.provider.Valid {
				return false, fmt.Errorf("subscription %s has incomplete typed creation fields", row.id)
			}
			continue
		}
		var creation Subscribe
		if err := decode(row.legacy, &creation); err != nil {
			return false, fmt.Errorf("subscription %s creation: %w", row.id, err)
		}
		if err := creation.Validate(); err != nil {
			return false, fmt.Errorf("subscription %s creation: %w", row.id, err)
		}
		if creation.IdempotencyKey != row.idempotency {
			return false, fmt.Errorf("subscription %s creation idempotency key does not match its relational key", row.id)
		}
		encoded, provider, err := encodeSubscriptionCreation(creation)
		if err != nil {
			return false, fmt.Errorf("subscription %s creation: %w", row.id, err)
		}
		if len(encoded) == 0 {
			return false, fmt.Errorf("subscription %s creation encoded as an empty protobuf", row.id)
		}
		updateIDs = append(updateIDs, row.id.String())
		providers = append(providers, provider)
		encodedCreations = append(encodedCreations, encoded)
	}
	if len(updateIDs) > 0 {
		result, err := tx.ExecContext(ctx, updateSubscriptionCreationsFromBatch,
			pq.Array(updateIDs), pq.Array(providers), pq.ByteaArray(encodedCreations))
		if err != nil {
			return false, err
		}
		updated, err := result.RowsAffected()
		if err != nil {
			return false, err
		}
		if updated != int64(len(updateIDs)) {
			return false, fmt.Errorf("subscription creation backfill updated %d of %d rows", updated, len(updateIDs))
		}
	}

	done := len(selected) == len(candidates) && len(candidates) < subscriptionCreationBackfillBatchSize
	if _, err := tx.ExecContext(ctx, `UPDATE notification_storage_backfill_progress
		SET subscription_creation_last_id = $1::uuid, subscription_creation_complete = $2 WHERE singleton`,
		selected[len(selected)-1].id.String(), done); err != nil {
		return false, err
	}
	return done, tx.Commit()
}

func subscriptionCreationBackfillCandidates(
	ctx context.Context, tx *sql.Tx, cursor sql.NullString,
) ([]subscriptionCreationBackfillCandidate, error) {
	query := subscriptionCreationCandidatesFromStart
	args := []any{subscriptionCreationBackfillBatchSize}
	if cursor.Valid {
		query = subscriptionCreationCandidatesAfterCursor
		args = []any{cursor.String, subscriptionCreationBackfillBatchSize}
	}
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var candidates []subscriptionCreationBackfillCandidate
	for rows.Next() {
		var candidate subscriptionCreationBackfillCandidate
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

func boundedSubscriptionCreationCandidates(
	candidates []subscriptionCreationBackfillCandidate,
) ([]subscriptionCreationBackfillCandidate, int64, error) {
	var selected []subscriptionCreationBackfillCandidate
	var total int64
	for _, candidate := range candidates {
		if len(selected) > 0 && total+candidate.serialized > subscriptionCreationBackfillBatchBytes {
			break
		}
		selected = append(selected, candidate)
		total += candidate.serialized
	}
	return selected, total, nil
}

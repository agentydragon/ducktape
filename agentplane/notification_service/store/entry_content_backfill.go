package store

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

const (
	entryContentBackfillBatchSize               = 512
	entryContentBackfillMaxBatchSize            = 4096
	entryContentBackfillBatchBytes        int64 = 1 << 20
	entryContentBackfillBatchTimeout            = 30 * time.Second
	entryContentBackfillValidationTimeout       = 5 * time.Minute
	entryContentBackfillCleanupTimeout          = 5 * time.Second
)

const entryContentBackfillAfter = `SELECT inbox_id, cursor,
	CASE WHEN payload IS NOT NULL AND ((identity_provider = 'actions' AND action_at IS NULL) OR
		(identity_provider = 'github_mergeability' AND mergeability_observed_at IS NULL) OR
		(identity_provider = 'message' AND message_body IS NULL))
		AND identity_provider IN ('actions', 'github_mergeability', 'message')
		THEN octet_length(payload::text)::bigint ELSE 0 END,
	payload IS NULL AND (action_state IS NOT NULL OR action_at IS NOT NULL OR action_actor_kind IS NOT NULL OR action_actor_namespace IS NOT NULL OR action_actor_name IS NOT NULL OR action_actor_issuer IS NOT NULL OR action_actor_subject IS NOT NULL OR mergeability_repository IS NOT NULL OR mergeability_base_ref IS NOT NULL OR mergeability_head_sha IS NOT NULL OR mergeability_base_sha IS NOT NULL OR mergeability_previous IS NOT NULL OR mergeability_current IS NOT NULL OR mergeability_observed_at IS NOT NULL OR mergeability_observed_at_submicro_nanos IS NOT NULL OR merge_commit_sha IS NOT NULL OR merge_commit_base_sha IS NOT NULL OR merge_commit_head_sha IS NOT NULL OR message_body IS NOT NULL OR message_from_session IS NOT NULL)
	FROM entry
	WHERE (inbox_id, cursor) > ($1, $2)
	ORDER BY inbox_id, cursor
	LIMIT $3`

const entryContentBackfillFromStart = `SELECT inbox_id, cursor,
	CASE WHEN payload IS NOT NULL AND ((identity_provider = 'actions' AND action_at IS NULL) OR
		(identity_provider = 'github_mergeability' AND mergeability_observed_at IS NULL) OR
		(identity_provider = 'message' AND message_body IS NULL))
		AND identity_provider IN ('actions', 'github_mergeability', 'message')
		THEN octet_length(payload::text)::bigint ELSE 0 END,
	payload IS NULL AND (action_state IS NOT NULL OR action_at IS NOT NULL OR action_actor_kind IS NOT NULL OR action_actor_namespace IS NOT NULL OR action_actor_name IS NOT NULL OR action_actor_issuer IS NOT NULL OR action_actor_subject IS NOT NULL OR mergeability_repository IS NOT NULL OR mergeability_base_ref IS NOT NULL OR mergeability_head_sha IS NOT NULL OR mergeability_base_sha IS NOT NULL OR mergeability_previous IS NOT NULL OR mergeability_current IS NOT NULL OR mergeability_observed_at IS NOT NULL OR mergeability_observed_at_submicro_nanos IS NOT NULL OR merge_commit_sha IS NOT NULL OR merge_commit_base_sha IS NOT NULL OR merge_commit_head_sha IS NOT NULL OR message_body IS NOT NULL OR message_from_session IS NOT NULL)
	FROM entry
	ORDER BY inbox_id, cursor
	LIMIT $1`

const entryContentBackfillLoad = `SELECT e.inbox_id, e.cursor, ` + eventIdentityReadColumns + `, e.payload::text
	FROM entry AS e
	JOIN unnest($1::uuid[], $2::bigint[]) AS candidates(inbox_id, cursor)
		ON e.inbox_id = candidates.inbox_id AND e.cursor = candidates.cursor
	WHERE e.identity_provider IN ('actions', 'github_mergeability', 'message')
		AND e.payload IS NOT NULL AND ((e.identity_provider = 'actions' AND e.action_at IS NULL) OR
			(e.identity_provider = 'github_mergeability' AND e.mergeability_observed_at IS NULL) OR
			(e.identity_provider = 'message' AND e.message_body IS NULL))
	ORDER BY e.inbox_id, e.cursor
	FOR UPDATE OF e`

const entryContentBackfillUpdate = `WITH contents AS (
	SELECT * FROM jsonb_to_recordset($1::jsonb) AS c(inbox_id UUID, cursor BIGINT,
		action_state INTEGER, action_at TIMESTAMPTZ, action_actor_kind VARCHAR, action_actor_namespace VARCHAR, action_actor_name VARCHAR, action_actor_issuer VARCHAR, action_actor_subject VARCHAR, mergeability_repository VARCHAR, mergeability_base_ref VARCHAR, mergeability_head_sha VARCHAR, mergeability_base_sha VARCHAR, mergeability_previous VARCHAR, mergeability_current VARCHAR, mergeability_observed_at TIMESTAMPTZ, mergeability_observed_at_submicro_nanos SMALLINT, merge_commit_sha VARCHAR, merge_commit_base_sha VARCHAR, merge_commit_head_sha VARCHAR, message_body TEXT, message_from_session TEXT)
)
UPDATE entry AS e SET action_state = contents.action_state, action_at = contents.action_at, action_actor_kind = contents.action_actor_kind, action_actor_namespace = contents.action_actor_namespace, action_actor_name = contents.action_actor_name, action_actor_issuer = contents.action_actor_issuer, action_actor_subject = contents.action_actor_subject, mergeability_repository = contents.mergeability_repository, mergeability_base_ref = contents.mergeability_base_ref, mergeability_head_sha = contents.mergeability_head_sha, mergeability_base_sha = contents.mergeability_base_sha, mergeability_previous = contents.mergeability_previous, mergeability_current = contents.mergeability_current, mergeability_observed_at = contents.mergeability_observed_at, mergeability_observed_at_submicro_nanos = contents.mergeability_observed_at_submicro_nanos, merge_commit_sha = contents.merge_commit_sha, merge_commit_base_sha = contents.merge_commit_base_sha, merge_commit_head_sha = contents.merge_commit_head_sha, message_body = contents.message_body, message_from_session = contents.message_from_session
FROM contents
WHERE e.inbox_id = contents.inbox_id AND e.cursor = contents.cursor AND e.payload IS NOT NULL
	AND ((e.identity_provider = 'actions' AND e.action_at IS NULL) OR
		(e.identity_provider = 'github_mergeability' AND e.mergeability_observed_at IS NULL) OR
		(e.identity_provider = 'message' AND e.message_body IS NULL))`

const entryContentExpiredUpdate = `UPDATE entry AS e SET action_state = NULL, action_at = NULL, action_actor_kind = NULL, action_actor_namespace = NULL, action_actor_name = NULL, action_actor_issuer = NULL, action_actor_subject = NULL, mergeability_repository = NULL, mergeability_base_ref = NULL, mergeability_head_sha = NULL, mergeability_base_sha = NULL, mergeability_previous = NULL, mergeability_current = NULL, mergeability_observed_at = NULL, mergeability_observed_at_submicro_nanos = NULL, merge_commit_sha = NULL, merge_commit_base_sha = NULL, merge_commit_head_sha = NULL, message_body = NULL, message_from_session = NULL
FROM unnest($1::uuid[], $2::bigint[]) AS expired(inbox_id, cursor)
WHERE e.inbox_id = expired.inbox_id AND e.cursor = expired.cursor AND e.payload IS NULL`

type entryContentBackfillCandidate struct {
	inboxID       uuid.UUID
	cursor        int64
	serializedLen int64
	clearExpired  bool
}

type entryContentBackfillKey struct {
	inboxID uuid.UUID
	cursor  int64
}

type entryContentBackfillRow struct {
	key      entryContentBackfillKey
	identity eventIdentityRead
	payload  []byte
	content  entryContentStorage
}

// BackfillEntryContent copies retained Action, mergeability and message payloads to native columns
// in bounded, resumable transactions. It scans all entries by the native primary key so progress
// cannot restart at a moving NULL prefix. GitHub webhook JSON remains in entry.payload and is never loaded by this
// command. All Notification writers, including cleanup workers, must run the native-content dual-write release first.
func BackfillEntryContent(ctx context.Context, db *sql.DB, batchSize int) (retErr error) {
	if batchSize < 1 || batchSize > entryContentBackfillMaxBatchSize {
		return fmt.Errorf("entry content backfill batch size must be between 1 and %d, got %d", entryContentBackfillMaxBatchSize, batchSize)
	}
	conn, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	var locked bool
	if err := conn.QueryRowContext(ctx, `SELECT pg_try_advisory_lock(hashtextextended('notification_entry_content_backfill', 0))`).Scan(&locked); err != nil {
		return fmt.Errorf("try entry content backfill lock: %w", err)
	}
	if !locked {
		return errors.New("entry content backfill is already running")
	}
	defer func() {
		unlockCtx, cancel := context.WithTimeout(context.Background(), entryContentBackfillCleanupTimeout)
		defer cancel()
		var unlocked bool
		unlockErr := conn.QueryRowContext(unlockCtx,
			`SELECT pg_advisory_unlock(hashtextextended('notification_entry_content_backfill', 0))`).Scan(&unlocked)
		if unlockErr == nil && unlocked {
			return
		}
		if unlockErr == nil {
			unlockErr = errors.New("PostgreSQL reported that the session did not own the entry content backfill lock")
		}
		discardErr := conn.Raw(func(any) error { return driver.ErrBadConn })
		retErr = errors.Join(retErr, fmt.Errorf("release entry content backfill lock; discarded pooled connection: %w", errors.Join(unlockErr, discardErr)))
	}()
	for {
		done, err := entryContentBackfillBatch(ctx, conn, batchSize)
		if err != nil {
			return err
		}
		if done {
			break
		}
	}
	if err := validateEntryContentBackfill(ctx, conn); err != nil {
		return err
	}
	return nil
}

func entryContentBackfillBatch(ctx context.Context, conn *sql.Conn, batchSize int) (bool, error) {
	batchCtx, cancel := context.WithTimeout(ctx, entryContentBackfillBatchTimeout)
	defer cancel()
	tx, err := conn.BeginTx(batchCtx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return false, fmt.Errorf("set entry content batch lock timeout: %w", err)
	}
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL statement_timeout = '25s'`); err != nil {
		return false, fmt.Errorf("set entry content batch statement timeout: %w", err)
	}
	if _, err := tx.ExecContext(batchCtx, `INSERT INTO notification_entry_content_backfill (singleton)
		VALUES (true) ON CONFLICT (singleton) DO NOTHING`); err != nil {
		return false, err
	}
	var inboxID sql.NullString
	var cursor sql.NullInt64
	var phase string
	if err := tx.QueryRowContext(batchCtx, `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_entry_content_backfill WHERE singleton FOR UPDATE`).Scan(&inboxID, &cursor, &phase); err != nil {
		return false, err
	}
	if inboxID.Valid != cursor.Valid {
		return false, errors.New("entry content backfill has an incomplete keyset cursor")
	}
	if inboxID.Valid {
		if _, err := uuid.Parse(inboxID.String); err != nil {
			return false, fmt.Errorf("entry content backfill cursor inbox %q: %w", inboxID.String, err)
		}
	}
	switch phase {
	case "backfilling":
		candidates, err := entryContentCandidates(batchCtx, tx, inboxID, cursor, batchSize)
		if err != nil {
			return false, err
		}
		if len(candidates) == 0 {
			if err := setEntryContentBackfillProgress(batchCtx, tx, "scanned", nil); err != nil {
				return false, err
			}
			return true, tx.Commit()
		}
		candidates = boundedEntryContentCandidates(candidates)
		if err := clearExpiredEntryContent(batchCtx, tx, candidates); err != nil {
			return false, err
		}
		rows, err := loadEntryContentBackfillRows(batchCtx, tx, candidates)
		if err != nil {
			return false, err
		}
		if err := updateEntryContentBackfillRows(batchCtx, tx, rows); err != nil {
			return false, err
		}
		last := candidates[len(candidates)-1]
		if err := setEntryContentBackfillProgress(batchCtx, tx, "backfilling", &last); err != nil {
			return false, err
		}
		return false, tx.Commit()
	case "scanned", "backfilled":
		return true, tx.Commit()
	default:
		return false, fmt.Errorf("unknown entry content backfill phase %q", phase)
	}
}

func entryContentCandidates(ctx context.Context, tx *sql.Tx, inboxID sql.NullString, cursor sql.NullInt64, batchSize int) ([]entryContentBackfillCandidate, error) {
	query := entryContentBackfillFromStart
	args := []any{batchSize}
	if inboxID.Valid {
		id, err := uuid.Parse(inboxID.String)
		if err != nil {
			return nil, fmt.Errorf("entry content backfill cursor inbox %q: %w", inboxID.String, err)
		}
		query = entryContentBackfillAfter
		args = []any{id, cursor.Int64, batchSize}
	}
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("scan entry content keys: %w", err)
	}
	defer rows.Close()
	var candidates []entryContentBackfillCandidate
	for rows.Next() {
		var candidate entryContentBackfillCandidate
		if err := rows.Scan(&candidate.inboxID, &candidate.cursor, &candidate.serializedLen, &candidate.clearExpired); err != nil {
			return nil, err
		}
		candidates = append(candidates, candidate)
	}
	return candidates, errors.Join(rows.Err(), rows.Close())
}

func clearExpiredEntryContent(ctx context.Context, tx *sql.Tx, candidates []entryContentBackfillCandidate) error {
	var ids []string
	var cursors []int64
	for _, candidate := range candidates {
		if !candidate.clearExpired {
			continue
		}
		ids = append(ids, candidate.inboxID.String())
		cursors = append(cursors, candidate.cursor)
	}
	if len(ids) == 0 {
		return nil
	}
	if _, err := tx.ExecContext(ctx, entryContentExpiredUpdate, pq.Array(ids), pq.Array(cursors)); err != nil {
		return fmt.Errorf("clear expired native entry content: %w", err)
	}
	return nil
}

func boundedEntryContentCandidates(candidates []entryContentBackfillCandidate) []entryContentBackfillCandidate {
	selected := make([]entryContentBackfillCandidate, 0, len(candidates))
	var totalBytes int64
	for _, candidate := range candidates {
		if candidate.serializedLen > entryContentBackfillBatchBytes-totalBytes {
			if len(selected) == 0 {
				// A single large retained payload can still make progress without exceeding the row bound.
				return append(selected, candidate)
			}
			break
		}
		selected = append(selected, candidate)
		totalBytes += candidate.serializedLen
	}
	return selected
}

func loadEntryContentBackfillRows(ctx context.Context, tx *sql.Tx, candidates []entryContentBackfillCandidate) ([]entryContentBackfillRow, error) {
	var ids []string
	var cursors []int64
	for _, candidate := range candidates {
		if candidate.serializedLen == 0 {
			continue
		}
		ids = append(ids, candidate.inboxID.String())
		cursors = append(cursors, candidate.cursor)
	}
	if len(ids) == 0 {
		return nil, nil
	}
	rows, err := tx.QueryContext(ctx, entryContentBackfillLoad, pq.Array(ids), pq.Array(cursors))
	if err != nil {
		return nil, fmt.Errorf("load selected native entry content: %w", err)
	}
	defer rows.Close()
	var loaded []entryContentBackfillRow
	for rows.Next() {
		var row entryContentBackfillRow
		destinations := append([]any{&row.key.inboxID, &row.key.cursor}, row.identity.destinations()...)
		destinations = append(destinations, &row.payload)
		if err := rows.Scan(destinations...); err != nil {
			return nil, err
		}
		event, err := row.identity.event()
		if err != nil {
			return nil, fmt.Errorf("inbox %s entry %d relational identity: %w", row.key.inboxID, row.key.cursor, err)
		}
		content, err := entryContentStorageFor(event, row.payload)
		if err != nil {
			return nil, fmt.Errorf("inbox %s entry %d native content: %w", row.key.inboxID, row.key.cursor, err)
		}
		row.content = content
		loaded = append(loaded, row)
	}
	return loaded, errors.Join(rows.Err(), rows.Close())
}

func updateEntryContentBackfillRows(ctx context.Context, tx *sql.Tx, rows []entryContentBackfillRow) error {
	if len(rows) == 0 {
		return nil
	}
	columns := strings.Split(strings.ReplaceAll(entryContentReadColumns, "\n", ""), ",")
	records := make([]map[string]any, len(rows))
	for i, row := range rows {
		record := map[string]any{"inbox_id": row.key.inboxID, "cursor": row.key.cursor}
		for column, value := range row.content.args() {
			record[strings.TrimSpace(columns[column])] = value
		}
		records[i] = record
	}
	encoded, err := json.Marshal(records)
	if err != nil {
		return fmt.Errorf("encode native content batch: %w", err)
	}
	result, err := tx.ExecContext(ctx, entryContentBackfillUpdate, string(encoded))
	if err != nil {
		return fmt.Errorf("bulk update native entry content: %w", err)
	}
	updated, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if updated != int64(len(rows)) {
		return fmt.Errorf("native content backfill updated %d rows, want %d", updated, len(rows))
	}
	return nil
}

func setEntryContentBackfillProgress(ctx context.Context, tx *sql.Tx, phase string, last *entryContentBackfillCandidate) error {
	if last == nil {
		_, err := tx.ExecContext(ctx, `UPDATE notification_entry_content_backfill
			SET phase = $1, last_inbox_id = NULL, last_cursor = NULL WHERE singleton`, phase)
		return err
	}
	_, err := tx.ExecContext(ctx, `UPDATE notification_entry_content_backfill
		SET phase = $1, last_inbox_id = $2, last_cursor = $3 WHERE singleton`, phase, last.inboxID, last.cursor)
	return err
}

func validateEntryContentBackfill(ctx context.Context, conn *sql.Conn) error {
	validationCtx, cancel := context.WithTimeout(ctx, entryContentBackfillValidationTimeout)
	defer cancel()
	tx, err := conn.BeginTx(validationCtx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(validationCtx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return err
	}
	if _, err := tx.ExecContext(validationCtx, `SET LOCAL statement_timeout = '5min'`); err != nil {
		return err
	}
	var phase string
	if err := tx.QueryRowContext(validationCtx, `SELECT phase FROM notification_entry_content_backfill WHERE singleton`).Scan(&phase); err != nil {
		return err
	}
	if phase != "scanned" {
		return fmt.Errorf("cannot complete entry content backfill in phase %q", phase)
	}
	var expired, missing bool
	if err := tx.QueryRowContext(validationCtx, `SELECT
		COALESCE(bool_or(payload IS NULL AND (action_state IS NOT NULL OR action_at IS NOT NULL OR action_actor_kind IS NOT NULL OR action_actor_namespace IS NOT NULL OR action_actor_name IS NOT NULL OR action_actor_issuer IS NOT NULL OR action_actor_subject IS NOT NULL OR mergeability_repository IS NOT NULL OR mergeability_base_ref IS NOT NULL OR mergeability_head_sha IS NOT NULL OR mergeability_base_sha IS NOT NULL OR mergeability_previous IS NOT NULL OR mergeability_current IS NOT NULL OR mergeability_observed_at IS NOT NULL OR mergeability_observed_at_submicro_nanos IS NOT NULL OR merge_commit_sha IS NOT NULL OR merge_commit_base_sha IS NOT NULL OR merge_commit_head_sha IS NOT NULL OR message_body IS NOT NULL OR message_from_session IS NOT NULL)), false),
		COALESCE(bool_or(payload IS NOT NULL AND ((identity_provider = 'actions' AND action_at IS NULL) OR
			(identity_provider = 'github_mergeability' AND mergeability_observed_at IS NULL) OR
			(identity_provider = 'message' AND message_body IS NULL))), false)
		FROM entry`).Scan(&expired, &missing); err != nil {
		return fmt.Errorf("verify entry content backfill completeness: %w", err)
	}
	if !expired && !missing {
		if _, err := tx.ExecContext(validationCtx, `UPDATE notification_entry_content_backfill
			SET phase = 'backfilled' WHERE singleton AND phase = 'scanned'`); err != nil {
			return fmt.Errorf("mark entry content backfill complete: %w", err)
		}
		return tx.Commit()
	}
	if _, err := tx.ExecContext(validationCtx, `UPDATE notification_entry_content_backfill
		SET phase = 'backfilling', last_inbox_id = NULL, last_cursor = NULL WHERE singleton`); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	issues := make([]string, 0, 2)
	if expired {
		issues = append(issues, "native content remains on entries whose JSON payload is NULL")
	}
	if missing {
		issues = append(issues, "native-source entries remain without native content")
	}
	return fmt.Errorf("entry content backfill is incomplete (%s); progress was reset to the beginning, confirm every writer and cleanup worker runs notification-entry-native-content-dual-write-v1 before retrying", strings.Join(issues, "; "))
}

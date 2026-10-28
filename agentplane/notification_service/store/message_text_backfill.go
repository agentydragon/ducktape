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
	messageTextBackfillBatchSize               = 512
	messageTextBackfillMaxBatchSize            = 4096
	messageTextBackfillBatchBytes        int64 = 1 << 20
	messageTextBackfillBatchTimeout            = 30 * time.Second
	messageTextBackfillValidationTimeout       = 5 * time.Minute
	messageTextBackfillCleanupTimeout          = 5 * time.Second
)

const messageTextBackfillAfter = `SELECT inbox_id, cursor,
	CASE WHEN identity_provider = 'message' AND payload IS NOT NULL AND message_body IS NULL
		THEN octet_length(payload::text)::bigint ELSE 0 END,
	identity_provider = 'message' AND payload IS NULL
		AND (message_body IS NOT NULL OR message_from_session IS NOT NULL)
	FROM entry
	WHERE (inbox_id, cursor) > ($1, $2)
	ORDER BY inbox_id, cursor
	LIMIT $3`

const messageTextBackfillFromStart = `SELECT inbox_id, cursor,
	CASE WHEN identity_provider = 'message' AND payload IS NOT NULL AND message_body IS NULL
		THEN octet_length(payload::text)::bigint ELSE 0 END,
	identity_provider = 'message' AND payload IS NULL
		AND (message_body IS NOT NULL OR message_from_session IS NOT NULL)
	FROM entry
	ORDER BY inbox_id, cursor
	LIMIT $1`

const messageTextBackfillLoad = `SELECT e.inbox_id, e.cursor, e.payload::text
	FROM entry AS e
	JOIN unnest($1::uuid[], $2::bigint[]) AS candidates(inbox_id, cursor)
		ON e.inbox_id = candidates.inbox_id AND e.cursor = candidates.cursor
	WHERE e.identity_provider = 'message' AND e.payload IS NOT NULL AND e.message_body IS NULL
	ORDER BY e.inbox_id, e.cursor
	FOR UPDATE OF e`

const messageTextBackfillUpdate = `WITH messages AS (
	SELECT inbox_id, cursor, body, has_from_session, from_session
	FROM unnest($1::uuid[], $2::bigint[], $3::text[], $4::boolean[], $5::text[])
		AS batch(inbox_id, cursor, body, has_from_session, from_session)
)
UPDATE entry AS e SET
	message_body = messages.body,
	message_from_session = CASE WHEN messages.has_from_session THEN messages.from_session ELSE NULL END
FROM messages
WHERE e.inbox_id = messages.inbox_id AND e.cursor = messages.cursor
	AND e.identity_provider = 'message' AND e.payload IS NOT NULL AND e.message_body IS NULL`

const messageTextBackfillExpiredUpdate = `WITH expired AS (
	SELECT inbox_id, cursor
	FROM unnest($1::uuid[], $2::bigint[]) AS batch(inbox_id, cursor)
)
UPDATE entry AS e SET message_body = NULL, message_from_session = NULL
FROM expired
WHERE e.inbox_id = expired.inbox_id AND e.cursor = expired.cursor
	AND e.identity_provider = 'message' AND e.payload IS NULL
	AND (e.message_body IS NOT NULL OR e.message_from_session IS NOT NULL)`

type messageTextBackfillCandidate struct {
	inboxID       uuid.UUID
	cursor        int64
	serializedLen int64
	clearExpired  bool
}

type messageTextBackfillKey struct {
	inboxID uuid.UUID
	cursor  int64
}

type messageTextBackfillRow struct {
	key  messageTextBackfillKey
	text storedMessageText
}

type storedMessageText struct {
	body        string
	fromSession *string
}

// BackfillMessageText copies retained Message JSON into its canonical text columns in bounded, resumable
// transactions. It scans every entry in native primary-key order, loading only Message payloads that still need
// conversion and clearing stale text wherever legacy JSON is already NULL. The serving readers and writers are
// not involved; deploy every append/replay/expiry writer at the text dual-write gate before running this command.
func BackfillMessageText(ctx context.Context, db *sql.DB, batchSize int) (retErr error) {
	if batchSize < 1 || batchSize > messageTextBackfillMaxBatchSize {
		return fmt.Errorf("message text backfill batch size must be between 1 and %d, got %d", messageTextBackfillMaxBatchSize, batchSize)
	}
	conn, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()

	// This must remain the first database operation. A later contract migration drops this checkpoint before
	// legacy JSON stops being written, making any stale copy of this command fail before it can inspect entries.
	checkpointCtx, cancelCheckpoint := context.WithTimeout(ctx, messageTextBackfillBatchTimeout)
	defer cancelCheckpoint()
	var phase string
	if err := conn.QueryRowContext(checkpointCtx, `SELECT phase FROM notification_message_text_backfill WHERE singleton`).Scan(&phase); err != nil {
		return fmt.Errorf("read message text backfill checkpoint before touching entries: %w", err)
	}
	if !validMessageTextBackfillPhase(phase) {
		return fmt.Errorf("unknown message text backfill phase %q", phase)
	}

	var locked bool
	if err := conn.QueryRowContext(checkpointCtx, `SELECT pg_try_advisory_lock(hashtextextended('notification_message_text_backfill', 0))`).Scan(&locked); err != nil {
		return fmt.Errorf("try message text backfill lock: %w", err)
	}
	if !locked {
		return errors.New("message text backfill is already running")
	}
	defer func() {
		unlockCtx, cancel := context.WithTimeout(context.Background(), messageTextBackfillCleanupTimeout)
		defer cancel()
		var unlocked bool
		unlockErr := conn.QueryRowContext(unlockCtx,
			`SELECT pg_advisory_unlock(hashtextextended('notification_message_text_backfill', 0))`).Scan(&unlocked)
		if unlockErr == nil && unlocked {
			return
		}
		if unlockErr == nil {
			unlockErr = errors.New("PostgreSQL reported that the session did not own the message text backfill lock")
		}
		discardErr := conn.Raw(func(any) error { return driver.ErrBadConn })
		retErr = errors.Join(retErr, fmt.Errorf("release message text backfill lock; discarded pooled connection: %w", errors.Join(unlockErr, discardErr)))
	}()

	for {
		done, err := messageTextBackfillBatch(ctx, conn, batchSize)
		if err != nil {
			return err
		}
		if done {
			break
		}
	}
	return validateMessageTextBackfill(ctx, conn)
}

func validMessageTextBackfillPhase(phase string) bool {
	return phase == "backfilling" || phase == "scanned" || phase == "backfilled"
}

func messageTextBackfillBatch(ctx context.Context, conn *sql.Conn, batchSize int) (bool, error) {
	batchCtx, cancel := context.WithTimeout(ctx, messageTextBackfillBatchTimeout)
	defer cancel()
	tx, err := conn.BeginTx(batchCtx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return false, fmt.Errorf("set message text batch lock timeout: %w", err)
	}
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL statement_timeout = '25s'`); err != nil {
		return false, fmt.Errorf("set message text batch statement timeout: %w", err)
	}
	var inboxID sql.NullString
	var cursor sql.NullInt64
	var phase string
	if err := tx.QueryRowContext(batchCtx, `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_message_text_backfill WHERE singleton FOR UPDATE`).Scan(&inboxID, &cursor, &phase); err != nil {
		return false, err
	}
	if inboxID.Valid != cursor.Valid {
		return false, errors.New("message text backfill has an incomplete keyset cursor")
	}
	if inboxID.Valid {
		if _, err := uuid.Parse(inboxID.String); err != nil {
			return false, fmt.Errorf("message text backfill cursor inbox %q: %w", inboxID.String, err)
		}
	}
	switch phase {
	case "backfilled", "scanned":
		return true, tx.Commit()
	case "backfilling":
	default:
		return false, fmt.Errorf("unknown message text backfill phase %q", phase)
	}

	candidates, err := messageTextBackfillCandidates(batchCtx, tx, inboxID, cursor, batchSize)
	if err != nil {
		return false, err
	}
	if len(candidates) == 0 {
		if _, err := tx.ExecContext(batchCtx, `UPDATE notification_message_text_backfill
			SET last_inbox_id = NULL, last_cursor = NULL, phase = 'scanned' WHERE singleton AND phase = 'backfilling'`); err != nil {
			return false, fmt.Errorf("mark Message text backfill scan complete: %w", err)
		}
		return true, tx.Commit()
	}
	candidates = boundedMessageTextBackfillCandidates(candidates)
	if err := clearExpiredMessageText(batchCtx, tx, candidates); err != nil {
		return false, err
	}
	rows, err := loadMessageTextBackfillRows(batchCtx, tx, candidates)
	if err != nil {
		return false, err
	}
	if err := updateMessageTextBackfillRows(batchCtx, tx, rows); err != nil {
		return false, err
	}
	last := candidates[len(candidates)-1]
	if _, err := tx.ExecContext(batchCtx, `UPDATE notification_message_text_backfill
		SET last_inbox_id = $1, last_cursor = $2, phase = 'backfilling' WHERE singleton`, last.inboxID, last.cursor); err != nil {
		return false, fmt.Errorf("advance message text backfill cursor: %w", err)
	}
	return false, tx.Commit()
}

func messageTextBackfillCandidates(ctx context.Context, tx *sql.Tx, inboxID sql.NullString, cursor sql.NullInt64, batchSize int) ([]messageTextBackfillCandidate, error) {
	query := messageTextBackfillFromStart
	args := []any{batchSize}
	if inboxID.Valid {
		id, err := uuid.Parse(inboxID.String)
		if err != nil {
			return nil, fmt.Errorf("message text backfill cursor inbox %q: %w", inboxID.String, err)
		}
		query = messageTextBackfillAfter
		args = []any{id, cursor.Int64, batchSize}
	}
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("scan message text keys: %w", err)
	}
	defer rows.Close()
	var candidates []messageTextBackfillCandidate
	for rows.Next() {
		var candidate messageTextBackfillCandidate
		if err := rows.Scan(&candidate.inboxID, &candidate.cursor, &candidate.serializedLen, &candidate.clearExpired); err != nil {
			return nil, err
		}
		candidates = append(candidates, candidate)
	}
	return candidates, errors.Join(rows.Err(), rows.Close())
}

func boundedMessageTextBackfillCandidates(candidates []messageTextBackfillCandidate) []messageTextBackfillCandidate {
	selected := make([]messageTextBackfillCandidate, 0, len(candidates))
	var totalBytes int64
	for _, candidate := range candidates {
		if candidate.serializedLen > messageTextBackfillBatchBytes-totalBytes {
			if len(selected) == 0 {
				// A single large but valid legacy document can make progress without exceeding the row bound.
				return append(selected, candidate)
			}
			break
		}
		selected = append(selected, candidate)
		totalBytes += candidate.serializedLen
	}
	return selected
}

func clearExpiredMessageText(ctx context.Context, tx *sql.Tx, candidates []messageTextBackfillCandidate) error {
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
	if _, err := tx.ExecContext(ctx, messageTextBackfillExpiredUpdate, pq.Array(ids), pq.Array(cursors)); err != nil {
		return fmt.Errorf("clear expired Message text: %w", err)
	}
	return nil
}

func loadMessageTextBackfillRows(ctx context.Context, tx *sql.Tx, candidates []messageTextBackfillCandidate) ([]messageTextBackfillRow, error) {
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
	rows, err := tx.QueryContext(ctx, messageTextBackfillLoad, pq.Array(ids), pq.Array(cursors))
	if err != nil {
		return nil, fmt.Errorf("load selected Message JSON: %w", err)
	}
	defer rows.Close()
	var loaded []messageTextBackfillRow
	for rows.Next() {
		var row messageTextBackfillRow
		var serialized []byte
		if err := rows.Scan(&row.key.inboxID, &row.key.cursor, &serialized); err != nil {
			return nil, err
		}
		row.text, err = decodeMessageTextPayload(serialized)
		if err != nil {
			return nil, fmt.Errorf("inbox %s entry %d Message text: %w", row.key.inboxID, row.key.cursor, err)
		}
		loaded = append(loaded, row)
	}
	return loaded, errors.Join(rows.Err(), rows.Close())
}

func decodeMessageTextPayload(payload []byte) (storedMessageText, error) {
	var legacy struct {
		Body        *string `json:"body"`
		FromSession *string `json:"from_session"`
	}
	if err := json.Unmarshal(payload, &legacy); err != nil {
		return storedMessageText{}, fmt.Errorf("decode legacy JSON: %w", err)
	}
	if legacy.Body == nil {
		return storedMessageText{}, errors.New("legacy JSON has no string body")
	}
	return storedMessageText{body: *legacy.Body, fromSession: legacy.FromSession}, nil
}

func updateMessageTextBackfillRows(ctx context.Context, tx *sql.Tx, rows []messageTextBackfillRow) error {
	if len(rows) == 0 {
		return nil
	}
	ids := make([]string, len(rows))
	cursors := make([]int64, len(rows))
	bodies := make([]string, len(rows))
	hasFromSession := make([]bool, len(rows))
	fromSessions := make([]string, len(rows))
	for i, row := range rows {
		ids[i] = row.key.inboxID.String()
		cursors[i] = row.key.cursor
		bodies[i] = row.text.body
		if row.text.fromSession != nil {
			hasFromSession[i] = true
			fromSessions[i] = *row.text.fromSession
		}
	}
	result, err := tx.ExecContext(ctx, messageTextBackfillUpdate,
		pq.Array(ids), pq.Array(cursors), pq.Array(bodies), pq.Array(hasFromSession), pq.Array(fromSessions))
	if err != nil {
		return fmt.Errorf("bulk update Message text: %w", err)
	}
	updated, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if updated != int64(len(rows)) {
		return fmt.Errorf("Message text backfill updated %d rows, want %d", updated, len(rows))
	}
	return nil
}

func validateMessageTextBackfill(ctx context.Context, conn *sql.Conn) error {
	validationCtx, cancel := context.WithTimeout(ctx, messageTextBackfillValidationTimeout)
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
	if err := tx.QueryRowContext(validationCtx, `SELECT phase FROM notification_message_text_backfill WHERE singleton FOR UPDATE`).Scan(&phase); err != nil {
		return err
	}
	if phase != "scanned" && phase != "backfilled" {
		return fmt.Errorf("cannot validate Message text backfill in phase %q; expected scanned or backfilled", phase)
	}
	var missing, expired, mismatch bool
	if err := tx.QueryRowContext(validationCtx, `SELECT
		COALESCE(bool_or(identity_provider = 'message' AND payload IS NOT NULL AND message_body IS NULL), false),
		COALESCE(bool_or(identity_provider = 'message' AND payload IS NULL
			AND (message_body IS NOT NULL OR message_from_session IS NOT NULL)), false),
		COALESCE(bool_or(identity_provider = 'message' AND payload IS NOT NULL AND (
			jsonb_typeof(payload) IS DISTINCT FROM 'object' OR
			jsonb_typeof(payload->'body') IS DISTINCT FROM 'string' OR
			payload->>'body' IS DISTINCT FROM message_body OR
			COALESCE(jsonb_typeof(payload->'from_session'), 'null') NOT IN ('null', 'string') OR
			payload->>'from_session' IS DISTINCT FROM message_from_session
		)), false)
		FROM entry`).Scan(&missing, &expired, &mismatch); err != nil {
		return fmt.Errorf("verify Message text backfill completeness: %w", err)
	}
	if !missing && !expired && !mismatch {
		if phase == "scanned" {
			if _, err := tx.ExecContext(validationCtx, `UPDATE notification_message_text_backfill
				SET phase = 'backfilled' WHERE singleton AND phase = 'scanned'`); err != nil {
				return fmt.Errorf("mark Message text backfill complete: %w", err)
			}
		}
		return tx.Commit()
	}
	if _, err := tx.ExecContext(validationCtx, `UPDATE notification_message_text_backfill
		SET phase = 'backfilling', last_inbox_id = NULL, last_cursor = NULL WHERE singleton`); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	issues := make([]string, 0, 3)
	if missing {
		issues = append(issues, "retained Message JSON remains without text columns")
	}
	if expired {
		issues = append(issues, "expired Message text remains after legacy JSON was cleared")
	}
	if mismatch {
		issues = append(issues, "Message text columns differ from the dual-written JSON payload")
	}
	return fmt.Errorf("Message text backfill is incomplete (%s); progress was reset to the beginning, confirm all append/replay/expiry writers and cleanup workers run notification-message-text-and-cleanup-dual-write-v1 before retrying",
		strings.Join(issues, "; "))
}

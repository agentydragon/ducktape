package store

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

const (
	messageLegacyCleanupBatchSize               = 512
	messageLegacyCleanupMaxBatchSize            = 4096
	messageLegacyCleanupBatchBytes        int64 = 1 << 20
	messageLegacyCleanupBatchTimeout            = 30 * time.Second
	messageLegacyCleanupValidationTimeout       = 5 * time.Minute
	messageLegacyCleanupReadTimeout             = 30 * time.Second
	messageLegacyCleanupReleaseTimeout          = 5 * time.Second
)

// The scan budgets the known Message payload shape without detoasting or sending its body to the command: six
// bytes per UTF-8 byte covers worst-case JSON string escaping, plus the stored JSONB size and fixed structure
// overhead. The actual body and optional sender columns are each bounded by validated text constraints.
const messageLegacyCleanupFromStart = `SELECT e.inbox_id, e.cursor,
	CASE WHEN e.identity_provider = 'message' AND (
		e.payload IS NOT NULL OR e.typed_payload IS NOT NULL OR
		(i.expired_through >= e.cursor AND (e.message_body IS NOT NULL OR e.message_from_session IS NOT NULL))
	) THEN
		COALESCE(pg_column_size(e.payload), 0)::bigint + COALESCE(octet_length(e.typed_payload), 0)::bigint +
		CASE WHEN e.payload IS NOT NULL THEN
			6 * (COALESCE(octet_length(e.message_body), 0) + COALESCE(octet_length(e.message_from_session), 0)) + 512
		ELSE 0 END +
		CASE WHEN i.expired_through >= e.cursor THEN
			COALESCE(octet_length(e.message_body), 0) + COALESCE(octet_length(e.message_from_session), 0)
		ELSE 0 END
	ELSE 0 END,
	COALESCE(e.identity_provider = 'message' AND (
		e.payload IS NOT NULL OR e.typed_payload IS NOT NULL OR
		(COALESCE(i.expired_through >= e.cursor, FALSE) AND (e.message_body IS NOT NULL OR e.message_from_session IS NOT NULL))
	), FALSE),
	COALESCE(e.identity_provider = 'message' AND e.message_body IS NULL AND
		(e.payload IS NOT NULL OR e.typed_payload IS NOT NULL) AND
		(i.expired_through IS NULL OR i.expired_through < e.cursor), FALSE)
	FROM entry AS e JOIN inbox AS i ON i.id = e.inbox_id
	ORDER BY e.inbox_id, e.cursor
	LIMIT $1`

const messageLegacyCleanupAfter = `SELECT e.inbox_id, e.cursor,
	CASE WHEN e.identity_provider = 'message' AND (
		e.payload IS NOT NULL OR e.typed_payload IS NOT NULL OR
		(i.expired_through >= e.cursor AND (e.message_body IS NOT NULL OR e.message_from_session IS NOT NULL))
	) THEN
		COALESCE(pg_column_size(e.payload), 0)::bigint + COALESCE(octet_length(e.typed_payload), 0)::bigint +
		CASE WHEN e.payload IS NOT NULL THEN
			6 * (COALESCE(octet_length(e.message_body), 0) + COALESCE(octet_length(e.message_from_session), 0)) + 512
		ELSE 0 END +
		CASE WHEN i.expired_through >= e.cursor THEN
			COALESCE(octet_length(e.message_body), 0) + COALESCE(octet_length(e.message_from_session), 0)
		ELSE 0 END
	ELSE 0 END,
	COALESCE(e.identity_provider = 'message' AND (
		e.payload IS NOT NULL OR e.typed_payload IS NOT NULL OR
		(COALESCE(i.expired_through >= e.cursor, FALSE) AND (e.message_body IS NOT NULL OR e.message_from_session IS NOT NULL))
	), FALSE),
	COALESCE(e.identity_provider = 'message' AND e.message_body IS NULL AND
		(e.payload IS NOT NULL OR e.typed_payload IS NOT NULL) AND
		(i.expired_through IS NULL OR i.expired_through < e.cursor), FALSE)
	FROM entry AS e JOIN inbox AS i ON i.id = e.inbox_id
	WHERE (e.inbox_id, e.cursor) > ($1, $2)
	ORDER BY e.inbox_id, e.cursor
	LIMIT $3`

const messageLegacyCleanupUpdate = `WITH batch AS (
	SELECT inbox_id, cursor
	FROM unnest($1::uuid[], $2::bigint[]) AS keys(inbox_id, cursor)
)
UPDATE entry AS e SET
	payload = NULL,
	typed_payload = NULL,
	message_body = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.message_body END,
	message_from_session = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.message_from_session END
FROM batch AS b JOIN inbox AS i ON i.id = b.inbox_id
WHERE e.inbox_id = b.inbox_id AND e.cursor = b.cursor AND e.identity_provider = 'message'
	AND (e.payload IS NOT NULL OR e.typed_payload IS NOT NULL OR
		(i.expired_through >= e.cursor AND (e.message_body IS NOT NULL OR e.message_from_session IS NOT NULL)))`

var messageLegacyCleanupConstraints = []string{
	"entry_message_legacy_content_absent",
	"entry_typed_payload_source_canonical",
	"entry_message_text_shape",
	"entry_message_text_body_ceiling",
	"entry_message_text_sender_length",
}

type messageLegacyCleanupCandidate struct {
	inboxID        uuid.UUID
	cursor         int64
	estimatedBytes int64
	needsCleanup   bool
	missingBody    bool
}

// CleanupMessageLegacyContent removes the retired JSON/protobuf Message representations after the TEXT-only
// readers and writers have rolled out. It preserves canonical TEXT on retained entries, clears all Message
// content inside each inbox's expired prefix, and never touches native GitHub payloads, Action/Mergeability
// content, event identities, receipts, or Message digests. The caller must pass the explicit release gate.
func CleanupMessageLegacyContent(ctx context.Context, db *sql.DB, batchSize int) (retErr error) {
	if batchSize < 1 || batchSize > messageLegacyCleanupMaxBatchSize {
		return fmt.Errorf("Message legacy cleanup batch size must be between 1 and %d, got %d", messageLegacyCleanupMaxBatchSize, batchSize)
	}
	readCtx, cancelRead := context.WithTimeout(ctx, messageLegacyCleanupReadTimeout)
	defer cancelRead()
	conn, err := db.Conn(readCtx)
	if err != nil {
		return err
	}
	defer conn.Close()

	// This checkpoint read must stay the first database operation. The final contract drops the table to fence
	// stale cleanup images before it removes the legacy Message columns.
	var phase string
	if err := conn.QueryRowContext(readCtx, `SELECT phase FROM notification_message_legacy_cleanup WHERE singleton`).Scan(&phase); err != nil {
		return fmt.Errorf("read Message legacy cleanup checkpoint before touching entries: %w", err)
	}
	if !validMessageLegacyCleanupPhase(phase) {
		return fmt.Errorf("unknown Message legacy cleanup phase %q", phase)
	}

	var locked bool
	if err := conn.QueryRowContext(readCtx, `SELECT pg_try_advisory_lock(hashtextextended('notification_message_legacy_cleanup', 0))`).Scan(&locked); err != nil {
		return fmt.Errorf("try Message legacy cleanup lock: %w", err)
	}
	if !locked {
		return errors.New("Message legacy cleanup is already running")
	}
	defer func() {
		unlockCtx, cancel := context.WithTimeout(context.Background(), messageLegacyCleanupReleaseTimeout)
		defer cancel()
		var unlocked bool
		unlockErr := conn.QueryRowContext(unlockCtx,
			`SELECT pg_advisory_unlock(hashtextextended('notification_message_legacy_cleanup', 0))`).Scan(&unlocked)
		if unlockErr == nil && unlocked {
			return
		}
		if unlockErr == nil {
			unlockErr = errors.New("PostgreSQL reported that the session did not own the Message legacy cleanup lock")
		}
		discardErr := conn.Raw(func(any) error { return driver.ErrBadConn })
		retErr = errors.Join(retErr, fmt.Errorf("release Message legacy cleanup lock; discarded pooled connection: %w", errors.Join(unlockErr, discardErr)))
	}()

	for {
		done, err := messageLegacyCleanupBatch(ctx, conn, batchSize)
		if err != nil {
			return err
		}
		if done {
			break
		}
	}
	return validateMessageLegacyCleanup(ctx, conn)
}

func validMessageLegacyCleanupPhase(phase string) bool {
	return phase == "cleaning" || phase == "scanned" || phase == "cleaned"
}

func messageLegacyCleanupBatch(ctx context.Context, conn *sql.Conn, batchSize int) (bool, error) {
	batchCtx, cancel := context.WithTimeout(ctx, messageLegacyCleanupBatchTimeout)
	defer cancel()
	tx, err := conn.BeginTx(batchCtx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return false, fmt.Errorf("set Message legacy cleanup lock timeout: %w", err)
	}
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL statement_timeout = '25s'`); err != nil {
		return false, fmt.Errorf("set Message legacy cleanup statement timeout: %w", err)
	}
	var lastInboxID sql.NullString
	var lastCursor sql.NullInt64
	var phase string
	if err := tx.QueryRowContext(batchCtx, `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_message_legacy_cleanup WHERE singleton FOR UPDATE`).Scan(&lastInboxID, &lastCursor, &phase); err != nil {
		return false, fmt.Errorf("read Message legacy cleanup progress: %w", err)
	}
	if lastInboxID.Valid != lastCursor.Valid {
		return false, errors.New("Message legacy cleanup has an incomplete keyset cursor")
	}
	if lastInboxID.Valid {
		if _, err := uuid.Parse(lastInboxID.String); err != nil {
			return false, fmt.Errorf("Message legacy cleanup cursor inbox %q: %w", lastInboxID.String, err)
		}
	}
	switch phase {
	case "cleaned", "scanned":
		return true, tx.Commit()
	case "cleaning":
	default:
		return false, fmt.Errorf("unknown Message legacy cleanup phase %q", phase)
	}

	candidates, err := messageLegacyCleanupCandidates(batchCtx, tx, lastInboxID, lastCursor, batchSize)
	if err != nil {
		return false, err
	}
	if len(candidates) == 0 {
		if _, err := tx.ExecContext(batchCtx, `UPDATE notification_message_legacy_cleanup
			SET last_inbox_id = NULL, last_cursor = NULL, phase = 'scanned'
			WHERE singleton AND phase = 'cleaning'`); err != nil {
			return false, fmt.Errorf("mark Message legacy cleanup scan complete: %w", err)
		}
		return true, tx.Commit()
	}
	candidates = boundedMessageLegacyCleanupCandidates(candidates)
	for _, candidate := range candidates {
		if candidate.missingBody {
			return false, fmt.Errorf("inbox %s entry %d has legacy Message content but no canonical body; legacy content was retained", candidate.inboxID, candidate.cursor)
		}
	}
	if err := clearMessageLegacyContent(batchCtx, tx, candidates); err != nil {
		return false, err
	}
	last := candidates[len(candidates)-1]
	if _, err := tx.ExecContext(batchCtx, `UPDATE notification_message_legacy_cleanup
		SET last_inbox_id = $1, last_cursor = $2, phase = 'cleaning' WHERE singleton AND phase = 'cleaning'`, last.inboxID, last.cursor); err != nil {
		return false, fmt.Errorf("advance Message legacy cleanup cursor: %w", err)
	}
	return false, tx.Commit()
}

func messageLegacyCleanupCandidates(
	ctx context.Context,
	tx *sql.Tx,
	inboxID sql.NullString,
	cursor sql.NullInt64,
	batchSize int,
) ([]messageLegacyCleanupCandidate, error) {
	query := messageLegacyCleanupFromStart
	args := []any{batchSize}
	if inboxID.Valid {
		id, err := uuid.Parse(inboxID.String)
		if err != nil {
			return nil, fmt.Errorf("Message legacy cleanup cursor inbox %q: %w", inboxID.String, err)
		}
		query = messageLegacyCleanupAfter
		args = []any{id, cursor.Int64, batchSize}
	}
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("scan native Message legacy cleanup keys: %w", err)
	}
	defer rows.Close()
	var candidates []messageLegacyCleanupCandidate
	for rows.Next() {
		var candidate messageLegacyCleanupCandidate
		if err := rows.Scan(&candidate.inboxID, &candidate.cursor, &candidate.estimatedBytes,
			&candidate.needsCleanup, &candidate.missingBody); err != nil {
			return nil, err
		}
		candidates = append(candidates, candidate)
	}
	return candidates, errors.Join(rows.Err(), rows.Close())
}

func boundedMessageLegacyCleanupCandidates(candidates []messageLegacyCleanupCandidate) []messageLegacyCleanupCandidate {
	selected := make([]messageLegacyCleanupCandidate, 0, len(candidates))
	var totalBytes int64
	for _, candidate := range candidates {
		if candidate.estimatedBytes > messageLegacyCleanupBatchBytes-totalBytes {
			if len(selected) == 0 {
				// An unusually large source row can make progress alone while keeping the batch's row bound.
				return append(selected, candidate)
			}
			break
		}
		selected = append(selected, candidate)
		totalBytes += candidate.estimatedBytes
	}
	return selected
}

func clearMessageLegacyContent(ctx context.Context, tx *sql.Tx, candidates []messageLegacyCleanupCandidate) error {
	ids := make([]string, 0, len(candidates))
	cursors := make([]int64, 0, len(candidates))
	for _, candidate := range candidates {
		if !candidate.needsCleanup {
			continue
		}
		ids = append(ids, candidate.inboxID.String())
		cursors = append(cursors, candidate.cursor)
	}
	if len(ids) == 0 {
		return nil
	}
	if _, err := tx.ExecContext(ctx, messageLegacyCleanupUpdate, pq.Array(ids), pq.Array(cursors)); err != nil {
		return fmt.Errorf("clear retired Message JSON and protobuf content: %w", err)
	}
	return nil
}

func validateMessageLegacyCleanup(ctx context.Context, conn *sql.Conn) error {
	validationCtx, cancel := context.WithTimeout(ctx, messageLegacyCleanupValidationTimeout)
	defer cancel()
	tx, err := conn.BeginTx(validationCtx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(validationCtx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return fmt.Errorf("set Message legacy cleanup validation lock timeout: %w", err)
	}
	if _, err := tx.ExecContext(validationCtx, `SET LOCAL statement_timeout = '5min'`); err != nil {
		return fmt.Errorf("set Message legacy cleanup validation statement timeout: %w", err)
	}
	var lastInboxID sql.NullString
	var lastCursor sql.NullInt64
	var phase string
	if err := tx.QueryRowContext(validationCtx, `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_message_legacy_cleanup WHERE singleton FOR UPDATE`).Scan(&lastInboxID, &lastCursor, &phase); err != nil {
		return fmt.Errorf("read Message legacy cleanup validation checkpoint: %w", err)
	}
	if phase != "scanned" && phase != "cleaned" {
		return fmt.Errorf("cannot validate Message legacy cleanup in phase %q; expected scanned or cleaned", phase)
	}

	if phase == "scanned" {
		var retainedMissingBody, legacyContent, expiredContent bool
		if err := tx.QueryRowContext(validationCtx, `SELECT
			EXISTS (SELECT 1 FROM entry AS e JOIN inbox AS i ON i.id = e.inbox_id
				WHERE e.identity_provider = 'message' AND (i.expired_through IS NULL OR i.expired_through < e.cursor) AND e.message_body IS NULL),
			EXISTS (SELECT 1 FROM entry WHERE identity_provider = 'message'
				AND (payload IS NOT NULL OR typed_payload IS NOT NULL)),
			EXISTS (SELECT 1 FROM entry AS e JOIN inbox AS i ON i.id = e.inbox_id
				WHERE e.identity_provider = 'message' AND i.expired_through >= e.cursor AND
					(e.payload IS NOT NULL OR e.typed_payload IS NOT NULL OR e.message_body IS NOT NULL OR e.message_from_session IS NOT NULL))`).
			Scan(&retainedMissingBody, &legacyContent, &expiredContent); err != nil {
			return fmt.Errorf("verify Message legacy cleanup results: %w", err)
		}
		if retainedMissingBody || legacyContent || expiredContent {
			if err := resetMessageLegacyCleanupProgress(validationCtx, tx); err != nil {
				return err
			}
			if err := tx.Commit(); err != nil {
				return err
			}
			issues := make([]string, 0, 3)
			if retainedMissingBody {
				issues = append(issues, "retained Message lacks canonical text")
			}
			if legacyContent {
				issues = append(issues, "Message JSON or protobuf remains")
			}
			if expiredContent {
				issues = append(issues, "expired Message content remains")
			}
			return fmt.Errorf("Message legacy cleanup verification failed (%s); checkpoint reset for a full retry", strings.Join(issues, "; "))
		}
	}

	if err := validateMessageLegacyCleanupConstraints(validationCtx, tx); err != nil {
		_ = tx.Rollback()
		if resetErr := resetMessageLegacyCleanup(ctx, conn); resetErr != nil {
			return errors.Join(fmt.Errorf("validate Message legacy cleanup constraints: %w", err), resetErr)
		}
		return fmt.Errorf("validate Message legacy cleanup constraints: %w; checkpoint reset for a full retry", err)
	}
	if _, err := tx.ExecContext(validationCtx, `UPDATE notification_message_legacy_cleanup
		SET phase = 'cleaned', last_inbox_id = NULL, last_cursor = NULL
		WHERE singleton AND phase IN ('scanned', 'cleaned')`); err != nil {
		return fmt.Errorf("mark Message legacy cleanup complete: %w", err)
	}
	return tx.Commit()
}

func validateMessageLegacyCleanupConstraints(ctx context.Context, tx *sql.Tx) error {
	rows, err := tx.QueryContext(ctx, `SELECT conname::text, convalidated FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname::text = ANY($1::text[])
		ORDER BY array_position($1::text[], conname::text)`, pq.Array(messageLegacyCleanupConstraints))
	if err != nil {
		return err
	}
	var pending []string
	found := make(map[string]bool, len(messageLegacyCleanupConstraints))
	for rows.Next() {
		var constraint string
		var validated bool
		if err := rows.Scan(&constraint, &validated); err != nil {
			rows.Close()
			return err
		}
		found[constraint] = true
		if !validated {
			pending = append(pending, constraint)
		}
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	var missing []string
	for _, constraint := range messageLegacyCleanupConstraints {
		if !found[constraint] {
			missing = append(missing, constraint)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("Message legacy cleanup proof constraints are missing: %s", strings.Join(missing, ", "))
	}
	if len(pending) == 0 {
		return nil
	}
	var ddl strings.Builder
	ddl.WriteString("ALTER TABLE entry ")
	for i, constraint := range pending {
		if i > 0 {
			ddl.WriteString(", ")
		}
		ddl.WriteString("VALIDATE CONSTRAINT ")
		ddl.WriteString(constraint)
	}
	if _, err := tx.ExecContext(ctx, ddl.String()); err != nil {
		return err
	}
	rows, err = tx.QueryContext(ctx, `SELECT conname::text, convalidated FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname::text = ANY($1::text[])`, pq.Array(messageLegacyCleanupConstraints))
	if err != nil {
		return err
	}
	validated := make(map[string]bool, len(messageLegacyCleanupConstraints))
	for rows.Next() {
		var constraint string
		var isValidated bool
		if err := rows.Scan(&constraint, &isValidated); err != nil {
			rows.Close()
			return err
		}
		validated[constraint] = isValidated
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	for _, constraint := range messageLegacyCleanupConstraints {
		if !validated[constraint] {
			return fmt.Errorf("Message legacy cleanup proof constraint %s is missing or not validated after validation", constraint)
		}
	}
	return nil
}

func resetMessageLegacyCleanupProgress(ctx context.Context, tx *sql.Tx) error {
	if _, err := tx.ExecContext(ctx, `UPDATE notification_message_legacy_cleanup
		SET phase = 'cleaning', last_inbox_id = NULL, last_cursor = NULL WHERE singleton`); err != nil {
		return fmt.Errorf("reset Message legacy cleanup progress: %w", err)
	}
	return nil
}

func resetMessageLegacyCleanup(ctx context.Context, conn *sql.Conn) error {
	resetCtx, cancel := context.WithTimeout(ctx, messageLegacyCleanupBatchTimeout)
	defer cancel()
	tx, err := conn.BeginTx(resetCtx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(resetCtx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return err
	}
	if _, err := tx.ExecContext(resetCtx, `SET LOCAL statement_timeout = '25s'`); err != nil {
		return err
	}
	if err := resetMessageLegacyCleanupProgress(resetCtx, tx); err != nil {
		return err
	}
	return tx.Commit()
}

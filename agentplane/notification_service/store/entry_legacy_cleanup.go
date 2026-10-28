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
	entryLegacyCleanupBatchSize               = 512
	entryLegacyCleanupMaxBatchSize            = 4096
	entryLegacyCleanupBatchBytes        int64 = 1 << 20
	entryLegacyCleanupBatchTimeout            = 30 * time.Second
	entryLegacyCleanupValidationTimeout       = 5 * time.Minute
	entryLegacyCleanupReadTimeout             = 30 * time.Second
	entryLegacyCleanupReleaseTimeout          = 5 * time.Second
)

// The scan visits entry primary keys in order and estimates the full row size only for rows it will update.
// Native GitHub webhook/observation JSON is never selected for cleanup.
const entryLegacyCleanupFromStart = `SELECT e.inbox_id, e.cursor,
    CASE WHEN (e.identity_provider IN ('actions', 'github_mergeability', 'message') AND (e.payload IS NOT NULL OR (i.expired_through >= e.cursor AND num_nonnulls(e.action_state, e.action_at, e.action_actor_kind, e.action_actor_namespace, e.action_actor_name, e.action_actor_issuer, e.action_actor_subject, e.mergeability_repository, e.mergeability_base_ref, e.mergeability_head_sha, e.mergeability_base_sha, e.mergeability_previous, e.mergeability_current, e.mergeability_observed_at, e.mergeability_observed_at_submicro_nanos, e.merge_commit_sha, e.merge_commit_base_sha, e.merge_commit_head_sha, e.message_body, e.message_from_session) > 0))) THEN pg_column_size(e)::bigint ELSE 0 END,
    (e.identity_provider IN ('actions', 'github_mergeability', 'message') AND (e.payload IS NOT NULL OR (i.expired_through >= e.cursor AND num_nonnulls(e.action_state, e.action_at, e.action_actor_kind, e.action_actor_namespace, e.action_actor_name, e.action_actor_issuer, e.action_actor_subject, e.mergeability_repository, e.mergeability_base_ref, e.mergeability_head_sha, e.mergeability_base_sha, e.mergeability_previous, e.mergeability_current, e.mergeability_observed_at, e.mergeability_observed_at_submicro_nanos, e.merge_commit_sha, e.merge_commit_base_sha, e.merge_commit_head_sha, e.message_body, e.message_from_session) > 0))),
    (e.identity_provider IN ('actions', 'github_mergeability', 'message') AND i.expired_through < e.cursor AND CASE e.identity_provider WHEN 'actions' THEN e.action_at IS NULL WHEN 'github_mergeability' THEN e.mergeability_observed_at IS NULL WHEN 'message' THEN e.message_body IS NULL ELSE FALSE END)
    FROM entry AS e JOIN inbox AS i ON i.id = e.inbox_id
    ORDER BY e.inbox_id, e.cursor LIMIT $1`

const entryLegacyCleanupAfter = `SELECT e.inbox_id, e.cursor,
    CASE WHEN (e.identity_provider IN ('actions', 'github_mergeability', 'message') AND (e.payload IS NOT NULL OR (i.expired_through >= e.cursor AND num_nonnulls(e.action_state, e.action_at, e.action_actor_kind, e.action_actor_namespace, e.action_actor_name, e.action_actor_issuer, e.action_actor_subject, e.mergeability_repository, e.mergeability_base_ref, e.mergeability_head_sha, e.mergeability_base_sha, e.mergeability_previous, e.mergeability_current, e.mergeability_observed_at, e.mergeability_observed_at_submicro_nanos, e.merge_commit_sha, e.merge_commit_base_sha, e.merge_commit_head_sha, e.message_body, e.message_from_session) > 0))) THEN pg_column_size(e)::bigint ELSE 0 END,
    (e.identity_provider IN ('actions', 'github_mergeability', 'message') AND (e.payload IS NOT NULL OR (i.expired_through >= e.cursor AND num_nonnulls(e.action_state, e.action_at, e.action_actor_kind, e.action_actor_namespace, e.action_actor_name, e.action_actor_issuer, e.action_actor_subject, e.mergeability_repository, e.mergeability_base_ref, e.mergeability_head_sha, e.mergeability_base_sha, e.mergeability_previous, e.mergeability_current, e.mergeability_observed_at, e.mergeability_observed_at_submicro_nanos, e.merge_commit_sha, e.merge_commit_base_sha, e.merge_commit_head_sha, e.message_body, e.message_from_session) > 0))),
    (e.identity_provider IN ('actions', 'github_mergeability', 'message') AND i.expired_through < e.cursor AND CASE e.identity_provider WHEN 'actions' THEN e.action_at IS NULL WHEN 'github_mergeability' THEN e.mergeability_observed_at IS NULL WHEN 'message' THEN e.message_body IS NULL ELSE FALSE END)
    FROM entry AS e JOIN inbox AS i ON i.id = e.inbox_id
    WHERE (e.inbox_id, e.cursor) > ($1, $2)
    ORDER BY e.inbox_id, e.cursor LIMIT $3`

const entryLegacyCleanupUpdate = `WITH batch AS (
    SELECT inbox_id, cursor FROM unnest($1::uuid[], $2::bigint[]) AS keys(inbox_id, cursor)
)
UPDATE entry AS e SET payload = NULL,
    action_state = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.action_state END,
    action_at = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.action_at END,
    action_actor_kind = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.action_actor_kind END,
    action_actor_namespace = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.action_actor_namespace END,
    action_actor_name = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.action_actor_name END,
    action_actor_issuer = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.action_actor_issuer END,
    action_actor_subject = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.action_actor_subject END,
    mergeability_repository = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.mergeability_repository END,
    mergeability_base_ref = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.mergeability_base_ref END,
    mergeability_head_sha = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.mergeability_head_sha END,
    mergeability_base_sha = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.mergeability_base_sha END,
    mergeability_previous = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.mergeability_previous END,
    mergeability_current = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.mergeability_current END,
    mergeability_observed_at = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.mergeability_observed_at END,
    mergeability_observed_at_submicro_nanos = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.mergeability_observed_at_submicro_nanos END,
    merge_commit_sha = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.merge_commit_sha END,
    merge_commit_base_sha = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.merge_commit_base_sha END,
    merge_commit_head_sha = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.merge_commit_head_sha END,
    message_body = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.message_body END,
    message_from_session = CASE WHEN i.expired_through >= e.cursor THEN NULL ELSE e.message_from_session END
FROM batch AS b JOIN inbox AS i ON i.id = b.inbox_id
WHERE e.inbox_id = b.inbox_id AND e.cursor = b.cursor AND e.identity_provider IN ('actions', 'github_mergeability', 'message')
    AND (e.payload IS NOT NULL OR (i.expired_through >= e.cursor AND num_nonnulls(e.action_state, e.action_at, e.action_actor_kind, e.action_actor_namespace, e.action_actor_name, e.action_actor_issuer, e.action_actor_subject, e.mergeability_repository, e.mergeability_base_ref, e.mergeability_head_sha, e.mergeability_base_sha, e.mergeability_previous, e.mergeability_current, e.mergeability_observed_at, e.mergeability_observed_at_submicro_nanos, e.merge_commit_sha, e.merge_commit_base_sha, e.merge_commit_head_sha, e.message_body, e.message_from_session) > 0))`

var entryLegacyCleanupConstraints = []string{
	"entry_action_content_source", "entry_mergeability_content_source", "entry_message_content_source",
	"entry_action_content_shape", "entry_mergeability_content_shape", "entry_message_content_shape",
	"entry_message_body_bytes", "entry_message_from_session_length", "entry_action_actor_length",
	"entry_owned_legacy_content_absent",
}

type entryLegacyCleanupCandidate struct {
	inboxID        uuid.UUID
	cursor         int64
	estimatedBytes int64
	needsCleanup   bool
	missingContent bool
}

// CleanupEntryLegacyContent removes retired JSON from Action, mergeability and Message entries after native-only
// readers and writers are deployed. It preserves native content on retained rows, clears it in expired prefixes,
// and never alters GitHub webhook/observation JSON, identities, receipts or Message digests.
func CleanupEntryLegacyContent(ctx context.Context, db *sql.DB, batchSize int) (retErr error) {
	if batchSize < 1 || batchSize > entryLegacyCleanupMaxBatchSize {
		return fmt.Errorf("Entry legacy cleanup batch size must be between 1 and %d, got %d", entryLegacyCleanupMaxBatchSize, batchSize)
	}
	readCtx, cancelRead := context.WithTimeout(ctx, entryLegacyCleanupReadTimeout)
	defer cancelRead()
	conn, err := db.Conn(readCtx)
	if err != nil {
		return err
	}
	defer conn.Close()

	// This checkpoint read must stay the first database operation. The final contract drops the table to fence stale cleanup images.
	var phase string
	if err := conn.QueryRowContext(readCtx, `SELECT phase FROM notification_entry_legacy_cleanup WHERE singleton`).Scan(&phase); err != nil {
		return fmt.Errorf("read Entry legacy cleanup checkpoint before touching entries: %w", err)
	}
	if !validEntryLegacyCleanupPhase(phase) {
		return fmt.Errorf("unknown Entry legacy cleanup phase %q", phase)
	}

	var locked bool
	if err := conn.QueryRowContext(readCtx, `SELECT pg_try_advisory_lock(hashtextextended('notification_entry_legacy_cleanup', 0))`).Scan(&locked); err != nil {
		return fmt.Errorf("try Entry legacy cleanup lock: %w", err)
	}
	if !locked {
		return errors.New("Entry legacy cleanup is already running")
	}
	defer func() {
		unlockCtx, cancel := context.WithTimeout(context.Background(), entryLegacyCleanupReleaseTimeout)
		defer cancel()
		var unlocked bool
		unlockErr := conn.QueryRowContext(unlockCtx,
			`SELECT pg_advisory_unlock(hashtextextended('notification_entry_legacy_cleanup', 0))`).Scan(&unlocked)
		if unlockErr == nil && unlocked {
			return
		}
		if unlockErr == nil {
			unlockErr = errors.New("PostgreSQL reported that the session did not own the Entry legacy cleanup lock")
		}
		discardErr := conn.Raw(func(any) error { return driver.ErrBadConn })
		retErr = errors.Join(retErr, fmt.Errorf("release Entry legacy cleanup lock; discarded pooled connection: %w", errors.Join(unlockErr, discardErr)))
	}()

	for {
		done, err := entryLegacyCleanupBatch(ctx, conn, batchSize)
		if err != nil {
			return err
		}
		if done {
			break
		}
	}
	return validateEntryLegacyCleanup(ctx, conn)
}

func validEntryLegacyCleanupPhase(phase string) bool {
	return phase == "cleaning" || phase == "scanned" || phase == "cleaned"
}

func entryLegacyCleanupBatch(ctx context.Context, conn *sql.Conn, batchSize int) (bool, error) {
	batchCtx, cancel := context.WithTimeout(ctx, entryLegacyCleanupBatchTimeout)
	defer cancel()
	tx, err := conn.BeginTx(batchCtx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return false, fmt.Errorf("set Entry legacy cleanup lock timeout: %w", err)
	}
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL statement_timeout = '25s'`); err != nil {
		return false, fmt.Errorf("set Entry legacy cleanup statement timeout: %w", err)
	}
	var lastInboxID sql.NullString
	var lastCursor sql.NullInt64
	var phase string
	if err := tx.QueryRowContext(batchCtx, `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_entry_legacy_cleanup WHERE singleton FOR UPDATE`).Scan(&lastInboxID, &lastCursor, &phase); err != nil {
		return false, fmt.Errorf("read Entry legacy cleanup progress: %w", err)
	}
	if lastInboxID.Valid != lastCursor.Valid {
		return false, errors.New("Entry legacy cleanup has an incomplete keyset cursor")
	}
	if lastInboxID.Valid {
		if _, err := uuid.Parse(lastInboxID.String); err != nil {
			return false, fmt.Errorf("Entry legacy cleanup cursor inbox %q: %w", lastInboxID.String, err)
		}
	}
	switch phase {
	case "cleaned", "scanned":
		return true, tx.Commit()
	case "cleaning":
	default:
		return false, fmt.Errorf("unknown Entry legacy cleanup phase %q", phase)
	}

	candidates, err := entryLegacyCleanupCandidates(batchCtx, tx, lastInboxID, lastCursor, batchSize)
	if err != nil {
		return false, err
	}
	if len(candidates) == 0 {
		if _, err := tx.ExecContext(batchCtx, `UPDATE notification_entry_legacy_cleanup
			SET last_inbox_id = NULL, last_cursor = NULL, phase = 'scanned'
			WHERE singleton AND phase = 'cleaning'`); err != nil {
			return false, fmt.Errorf("mark Entry legacy cleanup scan complete: %w", err)
		}
		return true, tx.Commit()
	}
	candidates = boundedEntryLegacyCleanupCandidates(candidates)
	for _, candidate := range candidates {
		if candidate.missingContent {
			return false, fmt.Errorf("inbox %s entry %d has legacy owned content but no canonical native fields; legacy content was retained", candidate.inboxID, candidate.cursor)
		}
	}
	if err := clearEntryLegacyContent(batchCtx, tx, candidates); err != nil {
		return false, err
	}
	last := candidates[len(candidates)-1]
	if _, err := tx.ExecContext(batchCtx, `UPDATE notification_entry_legacy_cleanup
		SET last_inbox_id = $1, last_cursor = $2, phase = 'cleaning' WHERE singleton AND phase = 'cleaning'`, last.inboxID, last.cursor); err != nil {
		return false, fmt.Errorf("advance Entry legacy cleanup cursor: %w", err)
	}
	return false, tx.Commit()
}

func entryLegacyCleanupCandidates(
	ctx context.Context,
	tx *sql.Tx,
	inboxID sql.NullString,
	cursor sql.NullInt64,
	batchSize int,
) ([]entryLegacyCleanupCandidate, error) {
	query := entryLegacyCleanupFromStart
	args := []any{batchSize}
	if inboxID.Valid {
		id, err := uuid.Parse(inboxID.String)
		if err != nil {
			return nil, fmt.Errorf("Entry legacy cleanup cursor inbox %q: %w", inboxID.String, err)
		}
		query = entryLegacyCleanupAfter
		args = []any{id, cursor.Int64, batchSize}
	}
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("scan native Entry legacy cleanup keys: %w", err)
	}
	defer rows.Close()
	var candidates []entryLegacyCleanupCandidate
	for rows.Next() {
		var candidate entryLegacyCleanupCandidate
		if err := rows.Scan(&candidate.inboxID, &candidate.cursor, &candidate.estimatedBytes,
			&candidate.needsCleanup, &candidate.missingContent); err != nil {
			return nil, err
		}
		candidates = append(candidates, candidate)
	}
	return candidates, errors.Join(rows.Err(), rows.Close())
}

func boundedEntryLegacyCleanupCandidates(candidates []entryLegacyCleanupCandidate) []entryLegacyCleanupCandidate {
	selected := make([]entryLegacyCleanupCandidate, 0, len(candidates))
	var totalBytes int64
	for _, candidate := range candidates {
		if candidate.estimatedBytes > entryLegacyCleanupBatchBytes-totalBytes {
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

func clearEntryLegacyContent(ctx context.Context, tx *sql.Tx, candidates []entryLegacyCleanupCandidate) error {
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
	if _, err := tx.ExecContext(ctx, entryLegacyCleanupUpdate, pq.Array(ids), pq.Array(cursors)); err != nil {
		return fmt.Errorf("clear retired owned JSON content: %w", err)
	}
	return nil
}

func validateEntryLegacyCleanup(ctx context.Context, conn *sql.Conn) error {
	validationCtx, cancel := context.WithTimeout(ctx, entryLegacyCleanupValidationTimeout)
	defer cancel()
	tx, err := conn.BeginTx(validationCtx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(validationCtx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return fmt.Errorf("set Entry legacy cleanup validation lock timeout: %w", err)
	}
	if _, err := tx.ExecContext(validationCtx, `SET LOCAL statement_timeout = '5min'`); err != nil {
		return fmt.Errorf("set Entry legacy cleanup validation statement timeout: %w", err)
	}
	var lastInboxID sql.NullString
	var lastCursor sql.NullInt64
	var phase string
	if err := tx.QueryRowContext(validationCtx, `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_entry_legacy_cleanup WHERE singleton FOR UPDATE`).Scan(&lastInboxID, &lastCursor, &phase); err != nil {
		return fmt.Errorf("read Entry legacy cleanup validation checkpoint: %w", err)
	}
	if phase != "scanned" && phase != "cleaned" {
		return fmt.Errorf("cannot validate Entry legacy cleanup in phase %q; expected scanned or cleaned", phase)
	}

	if phase == "scanned" {
		var retainedMissingContent, legacyContent, expiredContent bool
		if err := tx.QueryRowContext(validationCtx, `SELECT
			EXISTS (SELECT 1 FROM entry AS e JOIN inbox AS i ON i.id = e.inbox_id
				WHERE e.identity_provider IN ('actions', 'github_mergeability', 'message')
				AND i.expired_through < e.cursor AND CASE e.identity_provider
					WHEN 'actions' THEN e.action_at IS NULL
					WHEN 'github_mergeability' THEN e.mergeability_observed_at IS NULL
					WHEN 'message' THEN e.message_body IS NULL ELSE FALSE END),
			EXISTS (SELECT 1 FROM entry WHERE identity_provider IN ('actions', 'github_mergeability', 'message')
				AND payload IS NOT NULL),
			EXISTS (SELECT 1 FROM entry AS e JOIN inbox AS i ON i.id = e.inbox_id
				WHERE e.identity_provider IN ('actions', 'github_mergeability', 'message')
				AND i.expired_through >= e.cursor AND
				(e.payload IS NOT NULL OR num_nonnulls(e.action_state, e.action_at, e.action_actor_kind, e.action_actor_namespace, e.action_actor_name, e.action_actor_issuer, e.action_actor_subject, e.mergeability_repository, e.mergeability_base_ref, e.mergeability_head_sha, e.mergeability_base_sha, e.mergeability_previous, e.mergeability_current, e.mergeability_observed_at, e.mergeability_observed_at_submicro_nanos, e.merge_commit_sha, e.merge_commit_base_sha, e.merge_commit_head_sha, e.message_body, e.message_from_session) > 0))`).
			Scan(&retainedMissingContent, &legacyContent, &expiredContent); err != nil {
			return fmt.Errorf("verify owned entry legacy cleanup results: %w", err)
		}
		if retainedMissingContent || legacyContent || expiredContent {
			if err := resetEntryLegacyCleanupProgress(validationCtx, tx); err != nil {
				return err
			}
			if err := tx.Commit(); err != nil {
				return err
			}
			issues := make([]string, 0, 3)
			if retainedMissingContent {
				issues = append(issues, "retained owned entry lacks native content")
			}
			if legacyContent {
				issues = append(issues, "owned legacy JSON remains")
			}
			if expiredContent {
				issues = append(issues, "expired owned content remains")
			}
			return fmt.Errorf("entry legacy cleanup verification failed (%s); checkpoint reset for a full retry", strings.Join(issues, "; "))
		}
	}

	if err := validateEntryLegacyCleanupConstraints(validationCtx, tx); err != nil {
		_ = tx.Rollback()
		if resetErr := resetEntryLegacyCleanup(ctx, conn); resetErr != nil {
			return errors.Join(fmt.Errorf("validate Entry legacy cleanup constraints: %w", err), resetErr)
		}
		return fmt.Errorf("validate Entry legacy cleanup constraints: %w; checkpoint reset for a full retry", err)
	}
	if _, err := tx.ExecContext(validationCtx, `UPDATE notification_entry_legacy_cleanup
		SET phase = 'cleaned', last_inbox_id = NULL, last_cursor = NULL
		WHERE singleton AND phase IN ('scanned', 'cleaned')`); err != nil {
		return fmt.Errorf("mark Entry legacy cleanup complete: %w", err)
	}
	return tx.Commit()
}

func validateEntryLegacyCleanupConstraints(ctx context.Context, tx *sql.Tx) error {
	rows, err := tx.QueryContext(ctx, `SELECT conname::text, convalidated FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname::text = ANY($1::text[])
		ORDER BY array_position($1::text[], conname::text)`, pq.Array(entryLegacyCleanupConstraints))
	if err != nil {
		return err
	}
	var pending []string
	found := make(map[string]bool, len(entryLegacyCleanupConstraints))
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
	for _, constraint := range entryLegacyCleanupConstraints {
		if !found[constraint] {
			missing = append(missing, constraint)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("Entry legacy cleanup proof constraints are missing: %s", strings.Join(missing, ", "))
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
		WHERE conrelid = 'entry'::regclass AND conname::text = ANY($1::text[])`, pq.Array(entryLegacyCleanupConstraints))
	if err != nil {
		return err
	}
	validated := make(map[string]bool, len(entryLegacyCleanupConstraints))
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
	for _, constraint := range entryLegacyCleanupConstraints {
		if !validated[constraint] {
			return fmt.Errorf("Entry legacy cleanup proof constraint %s is missing or not validated after validation", constraint)
		}
	}
	return nil
}

func resetEntryLegacyCleanupProgress(ctx context.Context, tx *sql.Tx) error {
	if _, err := tx.ExecContext(ctx, `UPDATE notification_entry_legacy_cleanup
		SET phase = 'cleaning', last_inbox_id = NULL, last_cursor = NULL WHERE singleton`); err != nil {
		return fmt.Errorf("reset Entry legacy cleanup progress: %w", err)
	}
	return nil
}

func resetEntryLegacyCleanup(ctx context.Context, conn *sql.Conn) error {
	resetCtx, cancel := context.WithTimeout(ctx, entryLegacyCleanupBatchTimeout)
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
	if err := resetEntryLegacyCleanupProgress(resetCtx, tx); err != nil {
		return err
	}
	return tx.Commit()
}

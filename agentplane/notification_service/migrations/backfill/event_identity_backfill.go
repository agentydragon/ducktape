package backfill

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
	eventIdentityBackfillBatchSize            = 512
	eventIdentityBackfillMaxBatchSize         = 4096
	eventIdentityBackfillMaxBatchBytes  int64 = 4 << 20
	eventIdentityBackfillBatchTimeout         = 30 * time.Second
	eventIdentityBackfillCleanupTimeout       = 5 * time.Second
)

const eventIdentityBackfillSelect = `SELECT inbox_id, cursor, identity_provider IS NOT NULL,
	CASE WHEN identity_provider IS NULL THEN octet_length(event::text)::bigint ELSE 0 END
	FROM entry
	WHERE (inbox_id, cursor) > ($1, $2)
	ORDER BY inbox_id, cursor
	LIMIT $3
	FOR UPDATE`

const eventIdentityBackfillInitialSelect = `SELECT inbox_id, cursor, identity_provider IS NOT NULL,
	CASE WHEN identity_provider IS NULL THEN octet_length(event::text)::bigint ELSE 0 END
	FROM entry
	ORDER BY inbox_id, cursor
	LIMIT $1
	FOR UPDATE`

const eventIdentityBackfillLoad = `SELECT e.inbox_id, e.cursor, e.event::text
	FROM entry AS e
	JOIN unnest($1::uuid[], $2::bigint[]) AS candidates(inbox_id, cursor)
		ON e.inbox_id = candidates.inbox_id AND e.cursor = candidates.cursor
	WHERE e.identity_provider IS NULL`

const eventIdentityBackfillUpdate = `WITH identities AS (
	SELECT inbox_id, cursor, event
	FROM unnest($1::uuid[], $2::bigint[], $3::jsonb[]) AS batch(inbox_id, cursor, event)
)
UPDATE entry AS e SET
	identity_provider = CASE identities.event->>'provider'
		WHEN 'github_mergeability_snapshot' THEN 'github_mergeability'
		ELSE identities.event->>'provider'
	END,
	actions_request_id = CASE WHEN identities.event->>'provider' = 'actions'
		THEN (identities.event->>'request_id')::uuid END,
	actions_sequence = CASE WHEN identities.event->>'provider' = 'actions'
		THEN (identities.event->>'sequence')::bigint END,
	github_app_id = CASE WHEN identities.event->>'provider' IN ('github', 'github_observation', 'github_mergeability', 'github_mergeability_snapshot')
		THEN (identities.event->>'app_id')::bigint END,
	github_delivery_id = CASE WHEN identities.event->>'provider' = 'github'
		THEN (identities.event->>'delivery_id')::uuid END,
	github_repository_id = CASE WHEN identities.event->>'provider' IN ('github', 'github_observation', 'github_mergeability', 'github_mergeability_snapshot')
		THEN (identities.event->>'repository_id')::bigint END,
	github_event = CASE WHEN identities.event->>'provider' IN ('github', 'github_observation')
		THEN identities.event->>'event' END,
	github_action = CASE WHEN identities.event->>'provider' = 'github'
		THEN identities.event->>'action' END,
	github_pull_request_number = CASE WHEN identities.event->>'provider' IN ('github_mergeability', 'github_mergeability_snapshot')
		THEN (identities.event->>'pull_request_number')::bigint END,
	github_mergeability_sequence = CASE WHEN identities.event->>'provider' = 'github_mergeability'
		THEN (identities.event->>'sequence')::bigint END,
	github_snapshot_version = CASE WHEN identities.event->>'provider' = 'github_mergeability_snapshot'
		THEN (identities.event->>'snapshot_version')::bigint END,
	github_observation_sequence = CASE WHEN identities.event->>'provider' = 'github_observation'
		THEN (identities.event->>'sequence')::bigint END,
	message_id = CASE WHEN identities.event->>'provider' = 'message'
		THEN (identities.event->>'message_id')::uuid END,
	message_sender_namespace = CASE WHEN identities.event->>'provider' = 'message'
		THEN COALESCE(identities.event->'sender'->>'namespace', '') END,
	message_sender_name = CASE WHEN identities.event->>'provider' = 'message'
		THEN COALESCE(identities.event->'sender'->>'name', '') END
FROM identities
WHERE e.inbox_id = identities.inbox_id AND e.cursor = identities.cursor
	AND e.identity_provider IS NULL`

var eventIdentityIndexes = []struct {
	name string
	ddl  string
}{
	{
		name: "entry_identity_actions_key",
		ddl: `CREATE UNIQUE INDEX CONCURRENTLY entry_identity_actions_key
			ON entry (inbox_id, actions_request_id, actions_sequence)
			WHERE identity_provider = 'actions'`,
	},
	{
		name: "entry_identity_github_key",
		ddl: `CREATE UNIQUE INDEX CONCURRENTLY entry_identity_github_key
			ON entry (inbox_id, github_app_id, github_delivery_id, github_repository_id,
				github_event, github_action) NULLS NOT DISTINCT
			WHERE identity_provider = 'github'`,
	},
	{
		name: "entry_identity_github_observation_key",
		ddl: `CREATE UNIQUE INDEX CONCURRENTLY entry_identity_github_observation_key
			ON entry (inbox_id, github_app_id, github_repository_id, github_event, github_observation_sequence)
			WHERE identity_provider = 'github_observation'`,
	},
	{
		name: "entry_identity_mergeability_sequence_key",
		ddl: `CREATE UNIQUE INDEX CONCURRENTLY entry_identity_mergeability_sequence_key
			ON entry (inbox_id, github_app_id, github_repository_id, github_pull_request_number,
				github_mergeability_sequence)
			WHERE identity_provider = 'github_mergeability' AND github_mergeability_sequence IS NOT NULL`,
	},
	{
		name: "entry_identity_mergeability_snapshot_key",
		ddl: `CREATE UNIQUE INDEX CONCURRENTLY entry_identity_mergeability_snapshot_key
			ON entry (inbox_id, github_app_id, github_repository_id, github_pull_request_number,
				github_snapshot_version)
			WHERE identity_provider = 'github_mergeability' AND github_snapshot_version IS NOT NULL`,
	},
	{
		name: "entry_identity_message_key",
		ddl: `CREATE UNIQUE INDEX CONCURRENTLY entry_identity_message_key
			ON entry (inbox_id, message_id, message_sender_namespace, message_sender_name)
			WHERE identity_provider = 'message'`,
	},
}

// BackfillEventIdentities fills the relational natural key in bounded, resumable transactions, then builds its
// unique indexes outside serving readiness. All writers that populate the relational columns must be deployed
// before this command starts; the remaining legacy JSONB uniqueness index stays authoritative until cutover.
func BackfillEventIdentities(ctx context.Context, db *sql.DB, batchSize int) error {
	if batchSize < 1 {
		return fmt.Errorf("event identity backfill batch size must be positive, got %d", batchSize)
	}
	if batchSize > eventIdentityBackfillMaxBatchSize {
		return fmt.Errorf("event identity backfill batch size must not exceed %d, got %d", eventIdentityBackfillMaxBatchSize, batchSize)
	}
	return backfillEventIdentities(ctx, db, batchSize)
}

func backfillEventIdentities(ctx context.Context, db *sql.DB, batchSize int) (retErr error) {
	conn, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	var locked bool
	if err := conn.QueryRowContext(ctx, `SELECT pg_try_advisory_lock(hashtextextended('notification_event_identity_backfill', 0))`).Scan(&locked); err != nil {
		return fmt.Errorf("try event identity backfill lock: %w", err)
	}
	if !locked {
		return errors.New("event identity backfill is already running")
	}
	defer func() {
		unlockCtx, cancel := context.WithTimeout(context.Background(), eventIdentityBackfillCleanupTimeout)
		defer cancel()
		var unlocked bool
		unlockErr := conn.QueryRowContext(unlockCtx,
			`SELECT pg_advisory_unlock(hashtextextended('notification_event_identity_backfill', 0))`).Scan(&unlocked)
		if unlockErr == nil && unlocked {
			return
		}
		if unlockErr == nil {
			unlockErr = errors.New("PostgreSQL reported that the session did not own the event identity backfill lock")
		}
		discardErr := conn.Raw(func(any) error { return driver.ErrBadConn })
		retErr = errors.Join(retErr, fmt.Errorf("release event identity backfill lock; discarded pooled connection: %w", errors.Join(unlockErr, discardErr)))
	}()
	for {
		finished, err := backfillEventIdentityBatch(ctx, conn, batchSize)
		if err != nil {
			return err
		}
		if finished {
			break
		}
	}
	if err := reportUnconvertedEventIdentities(ctx, conn); err != nil {
		return err
	}
	if err := reportEventIdentityCollisions(ctx, conn); err != nil {
		return err
	}
	if err := validateEventIdentityShape(ctx, conn); err != nil {
		return err
	}
	if err := buildEventIdentityIndexes(ctx, conn); err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, `UPDATE notification_event_identity_backfill SET phase = 'indexed' WHERE singleton`)
	return err
}

func reportUnconvertedEventIdentities(ctx context.Context, conn *sql.Conn) error {
	const query = `SELECT inbox_id::text, cursor, count(*) OVER ()
		FROM entry WHERE identity_provider IS NULL ORDER BY inbox_id, cursor LIMIT 20`
	rows, err := conn.QueryContext(ctx, query)
	if err != nil {
		return fmt.Errorf("check for unconverted event identities: %w", err)
	}
	var total int64
	var sample []string
	for rows.Next() {
		var inboxID string
		var cursor int64
		if err := rows.Scan(&inboxID, &cursor, &total); err != nil {
			rows.Close()
			return err
		}
		sample = append(sample, fmt.Sprintf("%s/%d", inboxID, cursor))
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	if total > 0 {
		var phase string
		if err := conn.QueryRowContext(ctx, `SELECT phase FROM notification_event_identity_backfill WHERE singleton`).Scan(&phase); err != nil {
			return err
		}
		if phase == "backfilled" {
			if _, err := conn.ExecContext(ctx, `UPDATE notification_event_identity_backfill
				SET phase = 'backfilling', last_inbox_id = NULL, last_cursor = NULL WHERE singleton`); err != nil {
				return err
			}
			return fmt.Errorf("event identity backfill left %d entries unconverted (inbox/cursor examples: %s); progress restarted from the beginning, inspect their original JSON and migration progress before retrying", total, strings.Join(sample, ", "))
		}
		return fmt.Errorf("event identity backfill left %d entries unconverted after indexes reached phase %q (inbox/cursor examples: %s); retain the indexes and resolve the mixed-version writer state before retrying", total, phase, strings.Join(sample, ", "))
	}
	return nil
}

func validateEventIdentityShape(ctx context.Context, conn *sql.Conn) error {
	var validated bool
	if err := conn.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname = 'entry_identity_shape'`).Scan(&validated); err != nil {
		return fmt.Errorf("inspect entry identity shape constraint: %w", err)
	}
	if validated {
		return nil
	}
	return withEventIdentityLockTimeout(ctx, conn, "validate entry identity shape constraint", func() error {
		_, err := conn.ExecContext(ctx, `ALTER TABLE entry VALIDATE CONSTRAINT entry_identity_shape`)
		return err
	})
}

func withEventIdentityLockTimeout(ctx context.Context, conn *sql.Conn, operation string, run func() error) (retErr error) {
	if _, err := conn.ExecContext(ctx, `SET lock_timeout = '1s'`); err != nil {
		return fmt.Errorf("set lock timeout before %s: %w", operation, err)
	}
	defer func() {
		resetCtx, cancel := context.WithTimeout(context.Background(), eventIdentityBackfillCleanupTimeout)
		defer cancel()
		_, resetErr := conn.ExecContext(resetCtx, `RESET lock_timeout`)
		if resetErr == nil {
			return
		}
		discardErr := conn.Raw(func(any) error { return driver.ErrBadConn })
		retErr = errors.Join(retErr, fmt.Errorf("reset lock timeout after %s; discarded pooled connection: %w", operation, errors.Join(resetErr, discardErr)))
	}()
	return run()
}

// reportEventIdentityCollisions blocks natural-key index creation if legacy JSONB spellings decoded to the
// same source identity. It never deletes or chooses a winner: the inbox, cursors, and their original event JSON
// remain available for diagnosis before retrying.
func reportEventIdentityCollisions(ctx context.Context, conn *sql.Conn) error {
	const query = `WITH collisions AS (
		SELECT inbox_id, 'actions' AS provider, array_agg(cursor ORDER BY cursor) AS cursors
		FROM entry WHERE identity_provider = 'actions'
		GROUP BY inbox_id, actions_request_id, actions_sequence HAVING count(*) > 1
		UNION ALL
		SELECT inbox_id, 'github', array_agg(cursor ORDER BY cursor)
		FROM entry WHERE identity_provider = 'github'
		GROUP BY inbox_id, github_app_id, github_delivery_id, github_repository_id, github_event, github_action
		HAVING count(*) > 1
		UNION ALL
		SELECT inbox_id, 'github_observation', array_agg(cursor ORDER BY cursor)
		FROM entry WHERE identity_provider = 'github_observation'
		GROUP BY inbox_id, github_app_id, github_repository_id, github_event, github_observation_sequence
		HAVING count(*) > 1
		UNION ALL
		SELECT inbox_id, 'github_mergeability_sequence', array_agg(cursor ORDER BY cursor)
		FROM entry WHERE identity_provider = 'github_mergeability' AND github_mergeability_sequence IS NOT NULL
		GROUP BY inbox_id, github_app_id, github_repository_id, github_pull_request_number, github_mergeability_sequence
		HAVING count(*) > 1
		UNION ALL
		SELECT inbox_id, 'github_mergeability_snapshot', array_agg(cursor ORDER BY cursor)
		FROM entry WHERE identity_provider = 'github_mergeability' AND github_snapshot_version IS NOT NULL
		GROUP BY inbox_id, github_app_id, github_repository_id, github_pull_request_number, github_snapshot_version
		HAVING count(*) > 1
		UNION ALL
		SELECT inbox_id, 'message', array_agg(cursor ORDER BY cursor)
		FROM entry WHERE identity_provider = 'message'
		GROUP BY inbox_id, message_id, message_sender_namespace, message_sender_name HAVING count(*) > 1
	)
	SELECT inbox_id::text, provider, cursors::text, count(*) OVER ()
	FROM collisions ORDER BY inbox_id, provider LIMIT 20`
	rows, err := conn.QueryContext(ctx, query)
	if err != nil {
		return fmt.Errorf("check event identity collisions: %w", err)
	}
	var conflicts []string
	var total int64
	for rows.Next() {
		var inboxID, provider, cursors string
		if err := rows.Scan(&inboxID, &provider, &cursors, &total); err != nil {
			rows.Close()
			return err
		}
		conflicts = append(conflicts, fmt.Sprintf("inbox %s provider %s cursors %s", inboxID, provider, cursors))
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	if len(conflicts) > 0 {
		return fmt.Errorf("event identity backfill found %d natural-key collision groups; original entries were retained; first %d: %s", total, len(conflicts), strings.Join(conflicts, "; "))
	}
	return nil
}

type eventIdentityBackfillRow struct {
	inboxID     uuid.UUID
	cursor      int64
	identitySet bool
	eventBytes  int64
	event       []byte
}

type eventIdentityBackfillKey struct {
	inboxID uuid.UUID
	cursor  int64
}

func eventIdentityBackfillPrefix(candidates []eventIdentityBackfillRow) []eventIdentityBackfillRow {
	selected := make([]eventIdentityBackfillRow, 0, len(candidates))
	var totalBytes int64
	for _, row := range candidates {
		if row.eventBytes > eventIdentityBackfillMaxBatchBytes-totalBytes {
			if len(selected) == 0 {
				// Process a single oversize identity alone so forward progress does not depend on its size.
				return append(selected, row)
			}
			break
		}
		selected = append(selected, row)
		totalBytes += row.eventBytes
	}
	return selected
}

func backfillEventIdentityBatch(ctx context.Context, conn *sql.Conn, batchSize int) (bool, error) {
	batchCtx, cancel := context.WithTimeout(ctx, eventIdentityBackfillBatchTimeout)
	defer cancel()
	tx, err := conn.BeginTx(batchCtx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return false, fmt.Errorf("set event identity batch lock timeout: %w", err)
	}
	if _, err := tx.ExecContext(batchCtx, `SET LOCAL statement_timeout = '25s'`); err != nil {
		return false, fmt.Errorf("set event identity batch statement timeout: %w", err)
	}
	if _, err := tx.ExecContext(batchCtx, `INSERT INTO notification_event_identity_backfill (singleton)
		VALUES (true) ON CONFLICT (singleton) DO NOTHING`); err != nil {
		return false, err
	}
	var inboxID sql.NullString
	var cursor sql.NullInt64
	var phase string
	if err := tx.QueryRowContext(batchCtx, `SELECT last_inbox_id::text, last_cursor, phase
		FROM notification_event_identity_backfill WHERE singleton FOR UPDATE`).Scan(&inboxID, &cursor, &phase); err != nil {
		return false, err
	}
	if inboxID.Valid != cursor.Valid {
		return false, errors.New("event identity backfill has an incomplete keyset cursor")
	}
	if phase == "indexed" || phase == "backfilled" {
		if err := tx.Commit(); err != nil {
			return false, err
		}
		return true, nil
	}
	if phase != "backfilling" {
		return false, fmt.Errorf("unknown event identity backfill phase %q", phase)
	}
	var rows *sql.Rows
	if inboxID.Valid {
		lastInboxID, err := uuid.Parse(inboxID.String)
		if err != nil {
			return false, fmt.Errorf("event identity backfill cursor inbox %q: %w", inboxID.String, err)
		}
		rows, err = tx.QueryContext(batchCtx, eventIdentityBackfillSelect, lastInboxID, cursor.Int64, batchSize)
	} else {
		rows, err = tx.QueryContext(batchCtx, eventIdentityBackfillInitialSelect, batchSize)
	}
	if err != nil {
		return false, err
	}
	batch := make([]eventIdentityBackfillRow, 0, batchSize)
	for rows.Next() {
		var row eventIdentityBackfillRow
		if err := rows.Scan(&row.inboxID, &row.cursor, &row.identitySet, &row.eventBytes); err != nil {
			rows.Close()
			return false, err
		}
		batch = append(batch, row)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return false, err
	}
	if len(batch) == 0 {
		if _, err := tx.ExecContext(batchCtx, `UPDATE notification_event_identity_backfill SET phase = 'backfilled' WHERE singleton`); err != nil {
			return false, err
		}
		if err := tx.Commit(); err != nil {
			return false, err
		}
		return true, nil
	}

	batch = eventIdentityBackfillPrefix(batch)
	var inboxIDs []string
	var cursors []int64
	for _, row := range batch {
		if row.identitySet {
			continue
		}
		inboxIDs = append(inboxIDs, row.inboxID.String())
		cursors = append(cursors, row.cursor)
	}
	if len(inboxIDs) > 0 {
		rows, err := tx.QueryContext(batchCtx, eventIdentityBackfillLoad, pq.Array(inboxIDs), pq.Array(cursors))
		if err != nil {
			return false, fmt.Errorf("load selected event identities: %w", err)
		}
		loaded := make(map[eventIdentityBackfillKey][]byte, len(inboxIDs))
		for rows.Next() {
			var row eventIdentityBackfillRow
			if err := rows.Scan(&row.inboxID, &row.cursor, &row.event); err != nil {
				rows.Close()
				return false, err
			}
			loaded[eventIdentityBackfillKey{inboxID: row.inboxID, cursor: row.cursor}] = row.event
		}
		if err := errors.Join(rows.Err(), rows.Close()); err != nil {
			return false, err
		}
		var events []string
		for i := range batch {
			if batch[i].identitySet {
				continue
			}
			key := eventIdentityBackfillKey{inboxID: batch[i].inboxID, cursor: batch[i].cursor}
			batch[i].event = loaded[key]
			if batch[i].event == nil {
				return false, fmt.Errorf("selected event identity %s/%d disappeared during its locked batch", key.inboxID, key.cursor)
			}
			if _, err := DecodeHistoricalEventIdentity(batch[i].event); err != nil {
				return false, fmt.Errorf("inbox %s entry %d event identity: %w", batch[i].inboxID, batch[i].cursor, err)
			}
			events = append(events, string(batch[i].event))
		}
		result, err := tx.ExecContext(batchCtx, eventIdentityBackfillUpdate,
			pq.Array(inboxIDs), pq.Array(cursors), pq.Array(events))
		if err != nil {
			return false, err
		}
		updated, err := result.RowsAffected()
		if err != nil {
			return false, err
		}
		if updated != int64(len(inboxIDs)) {
			return false, fmt.Errorf("bulk backfill updated %d rows, want %d", updated, len(inboxIDs))
		}
	}
	last := batch[len(batch)-1]
	if _, err := tx.ExecContext(batchCtx, `UPDATE notification_event_identity_backfill
		SET last_inbox_id = $1, last_cursor = $2 WHERE singleton`, last.inboxID, last.cursor); err != nil {
		return false, err
	}
	if err := tx.Commit(); err != nil {
		return false, err
	}
	return false, nil
}

func buildEventIdentityIndexes(ctx context.Context, conn *sql.Conn) error {
	var phase string
	if err := conn.QueryRowContext(ctx, `SELECT phase FROM notification_event_identity_backfill WHERE singleton`).Scan(&phase); err != nil {
		return err
	}
	if phase == "indexed" {
		return nil
	}
	if phase != "backfilled" {
		return errors.New("cannot build event identity indexes before the backfill is complete")
	}
	for _, index := range eventIdentityIndexes {
		var valid, targetsEntry bool
		err := conn.QueryRowContext(ctx, `SELECT i.indisvalid, i.indrelid = 'entry'::regclass FROM pg_index i
			JOIN pg_class c ON c.oid = i.indexrelid
		JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE n.nspname = current_schema() AND c.relname = $1`, index.name).Scan(&valid, &targetsEntry)
		if err == nil {
			if !targetsEntry {
				return fmt.Errorf("index %s exists on a relation other than entry", index.name)
			}
		}
		if err == nil && valid {
			continue
		}
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("inspect index %s: %w", index.name, err)
		}
		if err == nil {
			if err := withEventIdentityLockTimeout(ctx, conn, "remove invalid index "+index.name, func() error {
				_, err := conn.ExecContext(ctx, `DROP INDEX CONCURRENTLY `+index.name)
				return err
			}); err != nil {
				return fmt.Errorf("remove invalid index %s: %w", index.name, err)
			}
		}
		if err := withEventIdentityLockTimeout(ctx, conn, "create index "+index.name, func() error {
			_, err := conn.ExecContext(ctx, index.ddl)
			return err
		}); err != nil {
			collisionErr := reportEventIdentityCollisions(ctx, conn)
			return fmt.Errorf("create index %s concurrently: %w", index.name, errors.Join(err, collisionErr))
		}
	}
	return nil
}

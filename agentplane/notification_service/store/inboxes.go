package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/google/uuid"
)

// Inbox is one Session's inbox: the entries of every source its subscriptions follow, in the order they were
// committed. A cursor is an entry's position, from 1.
type Inbox struct {
	ID             uuid.UUID
	DestinationRef DestinationRef
	SessionID      string
	// LastCursor is the cursor of the newest committed entry, or 0.
	LastCursor int64
	// Acknowledged moves only when the agent acknowledges.
	Acknowledged int64
	// Covered is the cursor up to which a notice names entries, independent of acknowledgement.
	Covered int64
	// ExpiredThrough is the cursor up to which payloads have expired.
	ExpiredThrough int64
	Retired        bool
	DeliveryError  *string
}

// Entry is one event delivered to an inbox.
type Entry struct {
	Cursor int64
	Event  EventIdentity
	// Payload is the provider's JSON object for the event.
	Payload json.RawMessage
	// SubscriptionIDs are the subscriptions the event matched, in ID order.
	SubscriptionIDs []uuid.UUID
}

// Notice is the latest message sent to an inbox's Session naming the entries that await the agent.
type Notice struct {
	// CommandID is the durable command ID the notice is submitted under, so a retry is the same command.
	CommandID     uuid.UUID
	ThroughCursor int64
	// Text is the exact message, fixed when the notice is prepared.
	Text      string
	Attempted bool
	Admitted  bool
	Confirmed bool
	Error     *string
	// RunnerCursor is the runner history cursor through which receipts have been copied.
	RunnerCursor int64
}

// InboxPage is entries after a cursor with the inbox they belong to and its latest notice.
type InboxPage struct {
	Inbox   Inbox
	Entries []Entry
	Notice  *Notice
}

const inboxColumns = `id, destination_ref, session_id, last_cursor, acknowledged, covered, expired_through, retired, delivery_error`

type rowScanner interface{ Scan(...any) error }

// scanInbox reads inboxColumns, then any columns the query adds into extra.
func scanInbox(row rowScanner, extra ...any) (Inbox, error) {
	var inbox Inbox
	var destination []byte
	var deliveryError sql.NullString
	if err := row.Scan(append([]any{
		&inbox.ID, &destination, &inbox.SessionID, &inbox.LastCursor, &inbox.Acknowledged,
		&inbox.Covered, &inbox.ExpiredThrough, &inbox.Retired, &deliveryError,
	}, extra...)...); err != nil {
		return Inbox{}, err
	}
	if err := decode(destination, &inbox.DestinationRef); err != nil {
		return Inbox{}, fmt.Errorf("inbox %s destination_ref: %w", inbox.ID, err)
	}
	if deliveryError.Valid {
		inbox.DeliveryError = &deliveryError.String
	}
	return inbox, nil
}

// owned is the owner's inbox, locked for update when lock is set. An inbox of another owner is not found.
func owned(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, owner Owner, lock bool) (Inbox, error) {
	query := `SELECT ` + inboxColumns + ` FROM inbox
		WHERE id = $1 AND owner_namespace = $2 AND owner_name = $3 AND destination_ref ->> 'uid' = $4`
	if lock {
		query += ` FOR UPDATE`
	}
	inbox, err := scanInbox(tx.QueryRowContext(ctx, query, inboxID, owner.Account.Namespace, owner.Account.Name, owner.SandboxUID))
	if errors.Is(err, sql.ErrNoRows) {
		return Inbox{}, ErrNotFound
	}
	return inbox, err
}

// ensureInbox is the owner's inbox for a destination Session, created if absent and returned locked for update.
func (s *Store) ensureInbox(ctx context.Context, tx *sql.Tx, owner action.ServiceAccount, ref DestinationRef, sessionID string) (Inbox, error) {
	written := now()
	key := DestinationKey(ref, sessionID)
	// Serialize only creation for an owner, to enforce the total destination quota.
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, owner.Namespace+"/"+owner.Name); err != nil {
		return Inbox{}, err
	}
	var existing uuid.UUID
	err := tx.QueryRowContext(ctx,
		`SELECT id FROM inbox WHERE owner_namespace = $1 AND owner_name = $2 AND destination_key = $3`,
		owner.Namespace, owner.Name, key).Scan(&existing)
	if errors.Is(err, sql.ErrNoRows) {
		var count int
		if err := tx.QueryRowContext(ctx, `
			SELECT count(*) FROM inbox WHERE owner_namespace = $1 AND owner_name = $2 AND destination_ref ->> 'uid' = $3`,
			owner.Namespace, owner.Name, ref.UID).Scan(&count); err != nil {
			return Inbox{}, err
		}
		if count >= s.quotas.InboxesPerAccount {
			return Inbox{}, &QuotaError{Quota: InboxesPerAccount, Limit: s.quotas.InboxesPerAccount}
		}
	} else if err != nil {
		return Inbox{}, err
	}
	destination, err := json.Marshal(ref)
	if err != nil {
		return Inbox{}, err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO inbox (id, owner_namespace, owner_name, destination_key, destination_ref, session_id,
			last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, $2, $3, $4, $5::jsonb, $6, 0, 0, 0, 0, false, $7, $7, $7)
		ON CONFLICT DO NOTHING`,
		uuid.New(), owner.Namespace, owner.Name, key, string(destination), sessionID, written); err != nil {
		return Inbox{}, err
	}
	inbox, err := scanInbox(tx.QueryRowContext(ctx,
		`SELECT `+inboxColumns+` FROM inbox WHERE owner_namespace = $1 AND owner_name = $2 AND destination_key = $3 FOR UPDATE`,
		owner.Namespace, owner.Name, key))
	if err != nil {
		return Inbox{}, err
	}
	if inbox.Retired {
		return Inbox{}, ErrInboxRetired
	}
	return inbox, nil
}

// ListInboxes lists the owner's inboxes in ID order, retired ones included, at most 1000.
func (s *Store) ListInboxes(ctx context.Context, owner Owner) ([]Inbox, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT `+inboxColumns+` FROM inbox
		WHERE owner_namespace = $1 AND owner_name = $2 AND destination_ref ->> 'uid' = $3 ORDER BY id LIMIT 1000`,
		owner.Account.Namespace, owner.Account.Name, owner.SandboxUID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var inboxes []Inbox
	for rows.Next() {
		inbox, err := scanInbox(rows)
		if err != nil {
			return nil, err
		}
		inboxes = append(inboxes, inbox)
	}
	return inboxes, rows.Err()
}

// Read returns up to limit entries with a cursor after both `after` and the inbox's expired prefix, oldest
// first, with the latest notice. The inbox is locked for the read, so entries and prefix metadata are one
// snapshot. Reading acknowledges nothing.
func (s *Store) Read(ctx context.Context, owner Owner, inboxID uuid.UUID, after int64, limit int) (InboxPage, error) {
	if after < 0 {
		return InboxPage{}, invalid("after_cursor must not be negative")
	}
	if limit < 1 || limit > 128 {
		return InboxPage{}, invalid("limit must be 1 to 128")
	}
	var page InboxPage
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		inbox, err := owned(ctx, tx, inboxID, owner, true)
		if err != nil {
			return err
		}
		page.Inbox = inbox
		entries, err := entriesAfter(ctx, tx, inbox, max(after, inbox.ExpiredThrough), limit)
		if err != nil {
			return err
		}
		page.Entries = entries
		notice, err := latestNotice(ctx, tx, inboxID)
		page.Notice = notice
		return err
	})
	return page, err
}

func entriesAfter(ctx context.Context, tx *sql.Tx, inbox Inbox, after int64, limit int) ([]Entry, error) {
	rows, err := tx.QueryContext(ctx,
		`SELECT cursor, event, payload FROM entry WHERE inbox_id = $1 AND cursor > $2 ORDER BY cursor LIMIT $3`,
		inbox.ID, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var entries []Entry
	for rows.Next() {
		var entry Entry
		var event, payload []byte
		if err := rows.Scan(&entry.Cursor, &event, &payload); err != nil {
			return nil, err
		}
		if entry.Event, err = DecodeEventIdentity(event); err != nil {
			return nil, fmt.Errorf("inbox %s entry %d event: %w", inbox.ID, entry.Cursor, err)
		}
		if payload == nil {
			return nil, fmt.Errorf("inbox %s entry %d: payload expired before the inbox's expired prefix", inbox.ID, entry.Cursor)
		}
		entry.Payload = payload
		entries = append(entries, entry)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(entries) == 0 {
		return nil, nil
	}
	matches, err := tx.QueryContext(ctx, `
		SELECT m.cursor, m.subscription_id FROM subscription_match m
		JOIN subscription s ON s.id = m.subscription_id
		WHERE s.inbox_id = $1 AND m.cursor BETWEEN $2 AND $3
		ORDER BY m.cursor, m.subscription_id`,
		inbox.ID, entries[0].Cursor, entries[len(entries)-1].Cursor)
	if err != nil {
		return nil, err
	}
	defer matches.Close()
	byCursor := make(map[int64][]uuid.UUID, len(entries))
	for matches.Next() {
		var cursor int64
		var subscriptionID uuid.UUID
		if err := matches.Scan(&cursor, &subscriptionID); err != nil {
			return nil, err
		}
		byCursor[cursor] = append(byCursor[cursor], subscriptionID)
	}
	for i := range entries {
		entries[i].SubscriptionIDs = byCursor[entries[i].Cursor]
	}
	return entries, matches.Err()
}

func latestNotice(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID) (*Notice, error) {
	var notice Notice
	var noticeError sql.NullString
	err := tx.QueryRowContext(ctx, `
		SELECT command_id, through_cursor, text, attempted, admitted, confirmed, error, runner_cursor
		FROM notice WHERE inbox_id = $1`, inboxID).Scan(&notice.CommandID, &notice.ThroughCursor, &notice.Text,
		&notice.Attempted, &notice.Admitted, &notice.Confirmed, &noticeError, &notice.RunnerCursor)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if noticeError.Valid {
		notice.Error = &noticeError.String
	}
	return &notice, nil
}

// Acknowledge marks the inbox handled through a cursor. It never moves backwards, and a cursor beyond the
// inbox's last entry fails with ErrCursorOutOfRange.
func (s *Store) Acknowledge(ctx context.Context, owner Owner, inboxID uuid.UUID, through int64) (Inbox, error) {
	if through < 0 {
		return Inbox{}, invalid("through_cursor must not be negative")
	}
	var acknowledged Inbox
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		inbox, err := owned(ctx, tx, inboxID, owner, true)
		if err != nil {
			return err
		}
		if through > inbox.LastCursor {
			return ErrCursorOutOfRange
		}
		acknowledged, err = scanInbox(tx.QueryRowContext(ctx,
			`UPDATE inbox SET acknowledged = GREATEST(acknowledged, $2), updated_at = $3 WHERE id = $1 RETURNING `+inboxColumns,
			inboxID, through, now()))
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return acknowledged, err
}

// Retire cancels the inbox's subscriptions and stops all work for it. Entries and payloads stay readable.
// Retiring a retired inbox changes nothing.
func (s *Store) Retire(ctx context.Context, owner Owner, inboxID uuid.UUID) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		inbox, err := owned(ctx, tx, inboxID, owner, true)
		if err != nil || inbox.Retired {
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE inbox SET retired = true, updated_at = $2, claim = NULL, next_attempt = NULL WHERE id = $1`,
			inboxID, now()); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE subscription SET cancelled = true, version = version + 1 WHERE inbox_id = $1 AND NOT cancelled`,
			inboxID); err != nil {
			return err
		}
		return notify(ctx, tx)
	})
}

// AppendEvent records an event of a subscription in its inbox: the inbox's entry for the event, appended at
// the next cursor unless it already holds one, and the subscription's match with it. The caller holds the
// inbox's row locked for update in tx.
func (s *Store) AppendEvent(ctx context.Context, tx *sql.Tx, inboxID, subscriptionID uuid.UUID, event EventIdentity, payload json.RawMessage) error {
	identity, err := json.Marshal(event)
	if err != nil {
		return err
	}
	var cursor int64
	err = tx.QueryRowContext(ctx, `SELECT cursor FROM entry WHERE inbox_id = $1 AND event = $2::jsonb`,
		inboxID, string(identity)).Scan(&cursor)
	if errors.Is(err, sql.ErrNoRows) {
		var messages int64
		if err := tx.QueryRowContext(ctx,
			`SELECT last_cursor, (SELECT count(*) FROM message WHERE inbox_id = $1) FROM inbox WHERE id = $1`,
			inboxID).Scan(&cursor, &messages); err != nil {
			return err
		}
		if cursor-messages >= int64(s.quotas.EntriesPerInbox) {
			return &QuotaError{Quota: EntriesPerInbox, Limit: s.quotas.EntriesPerInbox}
		}
		cursor++
		if _, err := tx.ExecContext(ctx, `UPDATE inbox SET last_cursor = $2 WHERE id = $1`, inboxID, cursor); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO entry (inbox_id, cursor, event, payload, created_at) VALUES ($1, $2, $3::jsonb, $4::jsonb, $5)`,
			inboxID, cursor, string(identity), string(payload), now()); err != nil {
			return err
		}
	} else if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx,
		`INSERT INTO subscription_match (subscription_id, cursor) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
		subscriptionID, cursor)
	return err
}

// Cleanup expires the payloads of entries older than 30 days and deletes inboxes retired that long ago, for
// up to 100 inboxes after afterID in ID order. It returns the last inbox it covered, or nil once there are no
// more. Entries keep their event identity, so a replay after payload expiry cannot notify again.
func (s *Store) Cleanup(ctx context.Context, afterID *uuid.UUID) (*uuid.UUID, error) {
	var last *uuid.UUID
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		cutoff := now().Add(-30 * 24 * time.Hour)
		rows, err := tx.QueryContext(ctx, `
			SELECT id, retired, updated_at, expired_through FROM inbox
			WHERE id > $1 ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED`, startAfter(afterID))
		if err != nil {
			return err
		}
		type candidate struct {
			id             uuid.UUID
			retired        bool
			updatedAt      time.Time
			expiredThrough int64
		}
		var candidates []candidate
		for rows.Next() {
			var c candidate
			if err := rows.Scan(&c.id, &c.retired, &c.updatedAt, &c.expiredThrough); err != nil {
				rows.Close()
				return err
			}
			candidates = append(candidates, c)
		}
		if err := errors.Join(rows.Err(), rows.Close()); err != nil {
			return err
		}
		for _, c := range candidates {
			id := c.id
			last = &id
			if c.retired && c.updatedAt.Before(cutoff) {
				if _, err := tx.ExecContext(ctx, `DELETE FROM inbox WHERE id = $1`, c.id); err != nil {
					return err
				}
				continue
			}
			if err := expirePayloads(ctx, tx, c.id, c.expiredThrough, cutoff); err != nil {
				return err
			}
		}
		return nil
	})
	return last, err
}

// expirePayloads drops the payloads of the inbox's oldest unexpired entries, at most 1000, up to the first one
// created since the cutoff, and moves the inbox's expired prefix over them.
func expirePayloads(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, expiredThrough int64, cutoff time.Time) error {
	rows, err := tx.QueryContext(ctx,
		`SELECT cursor, created_at FROM entry WHERE inbox_id = $1 AND cursor > $2 ORDER BY cursor LIMIT 1000`,
		inboxID, expiredThrough)
	if err != nil {
		return err
	}
	through := expiredThrough
	for rows.Next() {
		var cursor int64
		var createdAt time.Time
		if err := rows.Scan(&cursor, &createdAt); err != nil {
			rows.Close()
			return err
		}
		if !createdAt.Before(cutoff) {
			break
		}
		through = cursor
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	if through == expiredThrough {
		return nil
	}
	if _, err := tx.ExecContext(ctx, `UPDATE entry SET payload = NULL WHERE inbox_id = $1 AND cursor > $2 AND cursor <= $3`,
		inboxID, expiredThrough, through); err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE inbox SET expired_through = $2 WHERE id = $1`, inboxID, through)
	return err
}

package inbox

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"

	entrystorage "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox/internal/entry"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// Inbox is one Session's inbox: the entries of every source its subscriptions follow, in the order they were
// committed. A cursor is an entry's position, from 1.
type Inbox struct {
	ID            uuid.UUID
	OwnerIdentity model.Identity
	SessionID     string
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
	Event  sourceadapter.EventIdentity
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
	// HistoryCursor is the History cursor through which receipts have been copied.
	HistoryCursor int64
}

// InboxPage is entries after a cursor with the inbox they belong to and its latest notice.
type InboxPage struct {
	Inbox   Inbox
	Entries []Entry
	Notice  *Notice
}

const inboxColumns = `id, session_id, last_cursor, acknowledged, covered, expired_through, retired,
	delivery_error, owner_identity_name, owner_identity_uid`

type rowScanner interface{ Scan(...any) error }

// scanInbox reads inboxColumns, then any columns the query adds into extra.
func scanInbox(row rowScanner, extra ...any) (Inbox, error) {
	var inbox Inbox
	var deliveryError sql.NullString
	if err := row.Scan(append([]any{
		&inbox.ID, &inbox.SessionID, &inbox.LastCursor, &inbox.Acknowledged,
		&inbox.Covered, &inbox.ExpiredThrough, &inbox.Retired, &deliveryError, &inbox.OwnerIdentity.Name, &inbox.OwnerIdentity.UID,
	}, extra...)...); err != nil {
		return Inbox{}, err
	}
	if deliveryError.Valid {
		inbox.DeliveryError = &deliveryError.String
	}
	return inbox, nil
}

// owned is the owner's inbox, locked for update when lock is set. An inbox of another owner is not found.
func owned(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, owner Owner, lock bool) (Inbox, error) {
	predicate, args := ownerPredicate(owner, "", 2)
	query := `SELECT ` + inboxColumns + ` FROM inbox WHERE id = $1 AND ` + predicate
	if lock {
		query += ` FOR UPDATE`
	}
	inbox, err := scanInbox(tx.QueryRowContext(ctx, query, append([]any{inboxID}, args...)...))
	if errors.Is(err, sql.ErrNoRows) {
		return Inbox{}, ErrNotFound
	}
	return inbox, err
}

// InboxIDForSession resolves an inbox owned by the caller's exact execution Identity.
func (s *Store) InboxIDForSession(ctx context.Context, owner Owner, sessionID string) (uuid.UUID, error) {
	var id uuid.UUID
	predicate, args := ownerPredicate(owner, "", 2)
	err := s.db.QueryRowContext(ctx, `SELECT id FROM inbox WHERE session_id = $1 AND `+predicate, append([]any{sessionID}, args...)...).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return uuid.Nil, ErrNotFound
	}
	return id, err
}

// ensureInbox creates a Session-keyed inbox or locks that Session's inbox for its exact execution Identity.
func (s *Store) ensureInbox(ctx context.Context, tx transaction.Tx, owner Owner, sessionID string) (Inbox, error) {
	if owner.Identity.Name == "" || owner.Identity.UID == "" {
		return Inbox{}, ErrNotFound
	}
	written := now()
	// Serialize creation per Identity to enforce its inbox quota.
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, owner.Identity.Name+"/"+owner.Identity.UID); err != nil {
		return Inbox{}, err
	}
	var existing uuid.UUID
	identityPredicate, identityArgs := ownerPredicate(owner, "", 2)
	err := tx.QueryRowContext(ctx,
		`SELECT id FROM inbox WHERE session_id = $1 AND `+identityPredicate,
		append([]any{sessionID}, identityArgs...)...).Scan(&existing)
	if errors.Is(err, sql.ErrNoRows) {
		var count int
		if err := tx.QueryRowContext(ctx, `
			SELECT count(*) FROM inbox WHERE owner_identity_name = $1 AND owner_identity_uid = $2`,
			owner.Identity.Name, owner.Identity.UID).Scan(&count); err != nil {
			return Inbox{}, err
		}
		if count >= s.quotas.InboxesPerAccount {
			return Inbox{}, &QuotaError{Quota: InboxesPerAccount, Limit: s.quotas.InboxesPerAccount}
		}
	} else if err != nil {
		return Inbox{}, err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO inbox (id, session_id,
			last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until,
			owner_identity_name, owner_identity_uid)
		VALUES ($1, $2, 0, 0, 0, 0, false, $3, $3, $3, $4, $5)
		ON CONFLICT DO NOTHING`,
		uuid.New(), sessionID, written, owner.Identity.Name, owner.Identity.UID); err != nil {
		return Inbox{}, err
	}
	predicate, args := ownerPredicate(owner, "", 2)
	inbox, err := scanInbox(tx.QueryRowContext(ctx,
		`SELECT `+inboxColumns+` FROM inbox WHERE session_id = $1 AND `+predicate+` FOR UPDATE`,
		append([]any{sessionID}, args...)...))
	// Another Identity may already own this globally unique Session ID. Do not disclose it.
	if errors.Is(err, sql.ErrNoRows) {
		return Inbox{}, ErrNotFound
	}
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
	predicate, args := ownerPredicate(owner, "", 1)
	rows, err := s.db.QueryContext(ctx, `SELECT `+inboxColumns+` FROM inbox WHERE `+predicate+` ORDER BY id LIMIT 1000`, args...)
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
	return s.read(ctx, after, limit, func(tx transaction.Tx) (Inbox, error) {
		return owned(ctx, tx, inboxID, owner, true)
	})
}

// ReadSession gives the operator a non-destructive view of a retained inbox. The caller's database is
// already scoped to its Notification Service namespace, so the Session ID is the only row key needed.
// It does not depend on the current Sandbox or the continued existence of its ServiceAccount.
func (s *Store) ReadSession(ctx context.Context, sessionID string, after int64, limit int) (InboxPage, error) {
	return s.read(ctx, after, limit, func(tx transaction.Tx) (Inbox, error) {
		inbox, err := scanInbox(tx.QueryRowContext(ctx, `SELECT `+inboxColumns+` FROM inbox
			WHERE session_id = $1 FOR UPDATE`, sessionID))
		if errors.Is(err, sql.ErrNoRows) {
			return Inbox{}, ErrNotFound
		}
		return inbox, err
	})
}

func (s *Store) read(ctx context.Context, after int64, limit int, inboxForRead func(transaction.Tx) (Inbox, error)) (InboxPage, error) {
	if after < 0 {
		return InboxPage{}, model.Invalid("after_cursor must not be negative")
	}
	if limit < 1 || limit > 128 {
		return InboxPage{}, model.Invalid("limit must be 1 to 128")
	}
	var page InboxPage
	err := s.transaction(ctx, func(tx transaction.Tx) error {
		inbox, err := inboxForRead(tx)
		if err != nil {
			return err
		}
		page.Inbox = inbox
		entries, err := entriesAfter(ctx, tx, inbox, max(after, inbox.ExpiredThrough), limit)
		if err != nil {
			return err
		}
		page.Entries = entries
		notice, err := latestNotice(ctx, tx, inbox.ID)
		page.Notice = notice
		return err
	})
	return page, err
}

func entriesAfter(ctx context.Context, tx transaction.Tx, inbox Inbox, after int64, limit int) ([]Entry, error) {
	rows, err := tx.QueryContext(ctx,
		`SELECT cursor, `+storagecodec.EntryIdentityReadColumns+`,
			payload, `+storagecodec.EntryContentReadColumns+` FROM entry
			WHERE inbox_id = $1 AND cursor > $2 ORDER BY cursor LIMIT $3`,
		inbox.ID, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var entries []Entry
	for rows.Next() {
		var entry Entry
		var identity storagecodec.EntryIdentityStorage
		var payload []byte
		var content model.EntryContentStorage
		destinations := append([]any{&entry.Cursor}, storagecodec.EntryIdentityDestinations(&identity)...)
		destinations = append(destinations, &payload)
		destinations = append(destinations, storagecodec.EntryContentDestinations(&content)...)
		if err := rows.Scan(destinations...); err != nil {
			return nil, err
		}
		if entry.Event, err = sourceadapter.EventIdentityFromStorage(identity); err != nil {
			return nil, fmt.Errorf("inbox %s entry %d event: %w", inbox.ID, entry.Cursor, err)
		}
		entry.Payload, err = sourceadapter.EntryPayloadFromRead(entry.Event, payload, content)
		if err != nil {
			return nil, fmt.Errorf("inbox %s entry %d: %w", inbox.ID, entry.Cursor, err)
		}
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

func latestNotice(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID) (*Notice, error) {
	var notice Notice
	var noticeError sql.NullString
	err := tx.QueryRowContext(ctx, `
		SELECT command_id, through_cursor, text, attempted, admitted, confirmed, error, runner_cursor
		FROM notice WHERE inbox_id = $1`, inboxID).Scan(&notice.CommandID, &notice.ThroughCursor, &notice.Text,
		&notice.Attempted, &notice.Admitted, &notice.Confirmed, &noticeError, &notice.HistoryCursor)
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
		return Inbox{}, model.Invalid("through_cursor must not be negative")
	}
	var acknowledged Inbox
	err := s.transaction(ctx, func(tx transaction.Tx) error {
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
	return s.transaction(ctx, func(tx transaction.Tx) error {
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

// AppendTx records a prepared event of a subscription in its inbox: the inbox's entry for the event, appended at
// the next cursor unless it already holds one, and the subscription's match with it. The caller holds the
// inbox's row locked for update in tx.
func (s *Store) AppendTx(ctx context.Context, tx transaction.Tx, inboxID, subscriptionID uuid.UUID, input model.AppendInput) error {
	cursor, found, err := entrystorage.Lookup(ctx, tx, inboxID, input.Key)
	if err != nil {
		return err
	}
	if !found {
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
		if _, err := tx.ExecContext(ctx, `UPDATE inbox SET last_cursor = $2, next_attempt = least(next_attempt, now()) WHERE id = $1`, inboxID, cursor); err != nil {
			return err
		}
		if err := entrystorage.Insert(ctx, tx, inboxID, cursor, input, now()); err != nil {
			return err
		}
	}
	_, err = tx.ExecContext(ctx,
		`INSERT INTO subscription_match (subscription_id, cursor) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
		subscriptionID, cursor)
	return err
}

// Cleanup expires acknowledged payloads older than 30 days and deletes inboxes retired that long ago, for
// up to 100 inboxes after afterID in ID order. It returns the last inbox it covered, or nil once there are no
// more. Entries keep their event identity, so a replay after payload expiry cannot notify again.
func (s *Store) Cleanup(ctx context.Context, afterID *uuid.UUID) (*uuid.UUID, error) {
	var last *uuid.UUID
	err := s.transaction(ctx, func(tx transaction.Tx) error {
		cutoff := now().Add(-30 * 24 * time.Hour)
		rows, err := tx.QueryContext(ctx, `
			SELECT id, retired, updated_at, expired_through, acknowledged FROM inbox
			WHERE id > $1 ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED`, startAfter(afterID))
		if err != nil {
			return err
		}
		type candidate struct {
			id             uuid.UUID
			retired        bool
			updatedAt      time.Time
			expiredThrough int64
			acknowledged   int64
		}
		var candidates []candidate
		for rows.Next() {
			var c candidate
			if err := rows.Scan(&c.id, &c.retired, &c.updatedAt, &c.expiredThrough, &c.acknowledged); err != nil {
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
			if err := expirePayloads(ctx, tx, c.id, c.expiredThrough, c.acknowledged, cutoff); err != nil {
				return err
			}
		}
		return nil
	})
	return last, err
}

// expirePayloads drops the oldest acknowledged payloads, at most 1000, up to the first one
// created since the cutoff, and moves the inbox's expired prefix over them.
func expirePayloads(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, expiredThrough, acknowledged int64, cutoff time.Time) error {
	rows, err := tx.QueryContext(ctx,
		`SELECT cursor, created_at FROM entry WHERE inbox_id = $1 AND cursor > $2 AND cursor <= $3 ORDER BY cursor LIMIT 1000`,
		inboxID, expiredThrough, acknowledged)
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
	if _, err := tx.ExecContext(ctx, `UPDATE entry SET payload = NULL, action_state = NULL, action_at = NULL, action_actor_kind = NULL, action_actor_namespace = NULL, action_actor_name = NULL, action_actor_issuer = NULL, action_actor_subject = NULL, action_actor_identity_name = NULL, action_actor_identity_uid = NULL, mergeability_repository = NULL, mergeability_base_ref = NULL, mergeability_head_sha = NULL, mergeability_base_sha = NULL, mergeability_previous = NULL, mergeability_current = NULL, mergeability_observed_at = NULL, mergeability_observed_at_submicro_nanos = NULL, merge_commit_sha = NULL, merge_commit_base_sha = NULL, merge_commit_head_sha = NULL, message_body = NULL, message_from_session = NULL WHERE inbox_id = $1 AND cursor > $2 AND cursor <= $3`,
		inboxID, expiredThrough, through); err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE inbox SET expired_through = $2 WHERE id = $1`, inboxID, through)
	return err
}

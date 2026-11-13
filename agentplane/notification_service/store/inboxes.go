package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	entrystorage "github.com/agentydragon/ducktape/agentplane/notification_service/store/internal/entry"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// Inbox is one Session's inbox: the entries of every source its subscriptions follow, in the order they were
// committed. A cursor is an entry's position, from 1.
type Inbox struct {
	ID       uuid.UUID
	OwnerUID *string
	// OwnerIdentity is the Identity incarnation established as an owner, nil while none is.
	OwnerIdentity  *model.Identity
	DestinationRef model.DestinationRef
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
	Event  model.EventIdentity
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

const inboxColumns = `id, destination_namespace, destination_name, destination_uid,
	session_id, last_cursor, acknowledged, covered, expired_through, retired, delivery_error, owner_uid,
	owner_identity_name, owner_identity_uid`

type rowScanner interface{ Scan(...any) error }

// scanInbox reads inboxColumns, then any columns the query adds into extra.
func scanInbox(row rowScanner, extra ...any) (Inbox, error) {
	var inbox Inbox
	var namespace, name, uid sql.NullString
	var deliveryError, ownerUID, identityName, identityUID sql.NullString
	if err := row.Scan(append([]any{
		&inbox.ID, &namespace, &name, &uid, &inbox.SessionID, &inbox.LastCursor, &inbox.Acknowledged,
		&inbox.Covered, &inbox.ExpiredThrough, &inbox.Retired, &deliveryError, &ownerUID, &identityName, &identityUID,
	}, extra...)...); err != nil {
		return Inbox{}, err
	}
	var err error
	inbox.DestinationRef, err = destinationRefFromColumns(inbox.ID, namespace, name, uid)
	if err != nil {
		return Inbox{}, err
	}
	if ownerUID.Valid {
		inbox.OwnerUID = &ownerUID.String
	}
	if identityUID.Valid {
		inbox.OwnerIdentity = &model.Identity{Name: identityName.String, UID: identityUID.String}
	}
	if deliveryError.Valid {
		inbox.DeliveryError = &deliveryError.String
	}
	return inbox, nil
}

func destinationRefFromColumns(id uuid.UUID, namespace, name, uid sql.NullString) (model.DestinationRef, error) {
	if !namespace.Valid || !name.Valid || !uid.Valid {
		return model.DestinationRef{}, fmt.Errorf("inbox %s has incomplete normalized destination fields", id)
	}
	return model.DestinationRef{Namespace: namespace.String, Name: name.String, UID: uid.String}, nil
}

// owned is the owner's inbox, locked for update when lock is set. An inbox of another owner is not found.
func owned(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, owner Owner, lock bool) (Inbox, error) {
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

// InboxIDForSession resolves only an inbox owned by the current Sandbox incarnation.
func (s *Store) InboxIDForSession(ctx context.Context, owner Owner, sessionID string) (uuid.UUID, error) {
	var id uuid.UUID
	predicate, args := ownerPredicate(owner, "", 2)
	err := s.db.QueryRowContext(ctx, `SELECT id FROM inbox WHERE session_id = $1 AND `+predicate, append([]any{sessionID}, args...)...).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return uuid.Nil, ErrNotFound
	}
	return id, err
}

// ensureInbox is the owner's inbox for a destination Session, created if absent and returned locked
// for update. An IdentityOwner's inbox has no owner_uid, and its owner_namespace and owner_name name
// the destination Sandbox, as an AccountOwner's account does (a Sandbox runs as the ServiceAccount of
// its own name), so the inbox key and the inbox quota stay per Sandbox.
func (s *Store) ensureInbox(ctx context.Context, tx *sql.Tx, owner Owner, ref model.DestinationRef, sessionID string) (Inbox, error) {
	account := model.ServiceAccount{Namespace: ref.Namespace, Name: ref.Name}
	var ownerUID, identityName, identityUID any
	switch owner := owner.(type) {
	case AccountOwner:
		if owner.AccountUID == "" || owner.SandboxUID != ref.UID {
			return Inbox{}, ErrNotFound
		}
		account, ownerUID = owner.Account, owner.AccountUID
	case IdentityOwner:
		if owner.Identity.Name == "" || owner.Identity.UID == "" || owner.SandboxUID != ref.UID {
			return Inbox{}, ErrNotFound
		}
		identityName, identityUID = owner.Identity.Name, owner.Identity.UID
	default:
		panic(fmt.Sprintf("unknown inbox owner %T", owner))
	}
	written := now()
	key := model.DestinationKey(ref, sessionID)
	// Serialize only creation for an owner, to enforce the total destination quota.
	if _, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, account.Namespace+"/"+account.Name); err != nil {
		return Inbox{}, err
	}
	var existing uuid.UUID
	err := tx.QueryRowContext(ctx,
		`SELECT id FROM inbox WHERE owner_namespace = $1 AND owner_name = $2 AND destination_key = $3`,
		account.Namespace, account.Name, key).Scan(&existing)
	if errors.Is(err, sql.ErrNoRows) {
		var count int
		if err := tx.QueryRowContext(ctx, `
			SELECT count(*) FROM inbox WHERE owner_namespace = $1 AND owner_name = $2
				AND destination_uid = $3`,
			account.Namespace, account.Name, ref.UID).Scan(&count); err != nil {
			return Inbox{}, err
		}
		if count >= s.quotas.InboxesPerAccount {
			return Inbox{}, &QuotaError{Quota: InboxesPerAccount, Limit: s.quotas.InboxesPerAccount}
		}
	} else if err != nil {
		return Inbox{}, err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO inbox (id, owner_namespace, owner_name, destination_key,
			destination_namespace, destination_name, destination_uid, session_id,
			last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until, owner_uid,
			owner_identity_name, owner_identity_uid)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, 0, 0, 0, false, $9, $9, $9, $10, $11, $12)
		ON CONFLICT DO NOTHING`,
		uuid.New(), account.Namespace, account.Name, key, ref.Namespace, ref.Name, ref.UID, sessionID, written, ownerUID,
		identityName, identityUID); err != nil {
		return Inbox{}, err
	}
	predicate, args := ownerPredicate(owner, "", 2)
	inbox, err := scanInbox(tx.QueryRowContext(ctx,
		`SELECT `+inboxColumns+` FROM inbox WHERE destination_key = $1 AND `+predicate+` FOR UPDATE`,
		append([]any{key}, args...)...))
	// A different destination or owner may already have this globally unique Session ID.
	// Do not disclose that inbox or treat a conflicting creation as its owner.
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
	return s.read(ctx, after, limit, func(tx *sql.Tx) (Inbox, error) {
		return owned(ctx, tx, inboxID, owner, true)
	})
}

// ReadSession gives the operator a namespace-scoped, non-destructive view of a retained inbox.
// It does not depend on the current Sandbox or the continued existence of its ServiceAccount.
func (s *Store) ReadSession(ctx context.Context, namespace, sessionID string, after int64, limit int) (InboxPage, error) {
	return s.read(ctx, after, limit, func(tx *sql.Tx) (Inbox, error) {
		inbox, err := scanInbox(tx.QueryRowContext(ctx, `SELECT `+inboxColumns+` FROM inbox
			WHERE destination_namespace = $1 AND session_id = $2 FOR UPDATE`, namespace, sessionID))
		if errors.Is(err, sql.ErrNoRows) {
			return Inbox{}, ErrNotFound
		}
		return inbox, err
	})
}

func (s *Store) read(ctx context.Context, after int64, limit int, inboxForRead func(*sql.Tx) (Inbox, error)) (InboxPage, error) {
	if after < 0 {
		return InboxPage{}, model.Invalid("after_cursor must not be negative")
	}
	if limit < 1 || limit > 128 {
		return InboxPage{}, model.Invalid("limit must be 1 to 128")
	}
	var page InboxPage
	err := s.transaction(ctx, func(tx *sql.Tx) error {
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

func entriesAfter(ctx context.Context, tx *sql.Tx, inbox Inbox, after int64, limit int) ([]Entry, error) {
	rows, err := tx.QueryContext(ctx,
		`SELECT cursor, `+entrystorage.ReadColumns+`,
			CASE WHEN identity_provider IN ('github', 'github_observation') THEN payload END, `+storagecodec.EntryContentReadColumns+` FROM entry
			WHERE inbox_id = $1 AND cursor > $2 ORDER BY cursor LIMIT $3`,
		inbox.ID, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var entries []Entry
	for rows.Next() {
		var entry Entry
		var identity entrystorage.IdentityRow
		var payload []byte
		var content storagecodec.EntryContentStorage
		destinations := append([]any{&entry.Cursor}, identity.Destinations()...)
		destinations = append(destinations, &payload)
		destinations = append(destinations, content.Destinations()...)
		if err := rows.Scan(destinations...); err != nil {
			return nil, err
		}
		if entry.Event, err = identity.Event(); err != nil {
			return nil, fmt.Errorf("inbox %s entry %d event: %w", inbox.ID, entry.Cursor, err)
		}
		entry.Payload, err = storagecodec.EntryPayloadFromRead(entry.Event, payload, content)
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
		return Inbox{}, model.Invalid("through_cursor must not be negative")
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
func (s *Store) AppendEvent(ctx context.Context, tx *sql.Tx, inboxID, subscriptionID uuid.UUID, event model.EventIdentity, payload json.RawMessage) error {
	cursor, found, err := entrystorage.LookupIdentity(ctx, tx, inboxID, event)
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
		if _, err := tx.ExecContext(ctx, `UPDATE inbox SET last_cursor = $2 WHERE id = $1`, inboxID, cursor); err != nil {
			return err
		}
		if err := entrystorage.Insert(ctx, tx, inboxID, cursor, event, payload, now()); err != nil {
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
	err := s.transaction(ctx, func(tx *sql.Tx) error {
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
func expirePayloads(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, expiredThrough, acknowledged int64, cutoff time.Time) error {
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

// UnestablishedInboxes returns retained inboxes of owner's Sandbox incarnation whose owner of that
// kind is unknown: for an AccountOwner, its account's inboxes without a UID; for an IdentityOwner,
// those without either owner, since establishing the account owner also establishes any Identity.
// This metadata is for reconciliation, never for authorizing an owner read.
func (s *Store) UnestablishedInboxes(ctx context.Context, owner Owner) ([]Inbox, error) {
	var predicate string
	var args []any
	switch owner := owner.(type) {
	case AccountOwner:
		predicate = `owner_namespace = $1 AND owner_name = $2 AND destination_uid = $3 AND owner_uid IS NULL`
		args = []any{owner.Account.Namespace, owner.Account.Name, owner.SandboxUID}
	case IdentityOwner:
		predicate = `destination_uid = $1 AND owner_uid IS NULL AND owner_identity_uid IS NULL`
		args = []any{owner.SandboxUID}
	default:
		panic(fmt.Sprintf("unknown inbox owner %T", owner))
	}
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

// EstablishInboxOwner pins an unestablished inbox to its authoritative Session execution identity.
// The caller verifies that identity against the Session authority; every stored identity component
// must agree. An existing UID is immutable, and an absent inbox needs no reconciliation.
func (s *Store) EstablishInboxOwner(ctx context.Context, account model.ServiceAccount, ref model.DestinationRef, sessionID, uid string) error {
	if uid == "" {
		return ErrNotFound
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		var namespace, name string
		var destination model.DestinationRef
		var established sql.NullString
		err := tx.QueryRowContext(ctx, `SELECT owner_namespace, owner_name, destination_namespace,
			destination_name, destination_uid, owner_uid FROM inbox WHERE session_id = $1 FOR UPDATE`, sessionID).
			Scan(&namespace, &name, &destination.Namespace, &destination.Name, &destination.UID, &established)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if account.Namespace != namespace || account.Name != name || destination != ref || (established.Valid && established.String != uid) {
			return ErrNotFound
		}
		if established.Valid {
			return nil
		}
		_, err = tx.ExecContext(ctx, `UPDATE inbox SET owner_uid = $2 WHERE session_id = $1 AND owner_uid IS NULL`, sessionID, uid)
		return err
	})
}

// EstablishInboxIdentity pins an inbox to the Identity incarnation the Session authority recorded the
// Session as executing as. The caller verifies that Identity and the Session's destination against the
// Session authority. An established Identity is immutable, and an absent inbox needs no reconciliation.
func (s *Store) EstablishInboxIdentity(ctx context.Context, ref model.DestinationRef, sessionID string, identity model.Identity) error {
	if identity.Name == "" || identity.UID == "" {
		return ErrNotFound
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		var destination model.DestinationRef
		var name, uid sql.NullString
		err := tx.QueryRowContext(ctx, `SELECT destination_namespace, destination_name, destination_uid,
			owner_identity_name, owner_identity_uid FROM inbox WHERE session_id = $1 FOR UPDATE`, sessionID).
			Scan(&destination.Namespace, &destination.Name, &destination.UID, &name, &uid)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if destination != ref || uid.Valid && (model.Identity{Name: name.String, UID: uid.String}) != identity {
			return ErrNotFound
		}
		if uid.Valid {
			return nil
		}
		_, err = tx.ExecContext(ctx, `UPDATE inbox SET owner_identity_name = $2, owner_identity_uid = $3
			WHERE session_id = $1 AND owner_identity_uid IS NULL`, sessionID, identity.Name, identity.UID)
		return err
	})
}

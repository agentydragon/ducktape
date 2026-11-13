package store

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	entrystorage "github.com/agentydragon/ducktape/agentplane/notification_service/store/internal/entry"
	"github.com/google/uuid"
)

// MessageBodyCeiling is the most UTF-8 bytes a message body can hold: the database refuses more, and the
// service's configured limit is at most this.
const MessageBodyCeiling = 256 * 1024

// ErrNoInbox is a message to a Session whose inbox does not exist yet. OpenInbox creates it once the caller has
// confirmed the Session runs in the destination Sandbox.
var ErrNoInbox = errors.New("the Session has no inbox yet")

// Message is a message a workload sends to a Session's inbox.
type Message struct {
	Sender Sender
	// Recipient owns the inbox: the ServiceAccount or, for a Session the Session authority recorded as
	// executing as an Identity alone, the Identity incarnation the destination Session executes as.
	Recipient      Owner
	DestinationRef model.DestinationRef
	SessionID      string
	// IdempotencyKey is unique per sender and inbox: an identical retry answers with the message it sent.
	IdempotencyKey string
	Body           string
	// FromSession is the sender's own label for the Session it writes from, which nothing verifies.
	FromSession *string
}

// Sender is who sends a message: an AccountSender or an IdentitySender.
type Sender interface{ isSender() }

// AccountSender is a ServiceAccount incarnation.
type AccountSender struct {
	Account model.ServiceAccount
	UID     string
}

// IdentitySender is an Identity incarnation sending from the Sandbox its workload credential is
// controlled by. Recipients see the message as from the Identity and that Sandbox's name, the
// ServiceAccount the Sandbox ran as before it ran as an Identity.
type IdentitySender struct {
	Identity model.Identity
	Sandbox  model.ServiceAccount
}

func (AccountSender) isSender()  {}
func (IdentitySender) isSender() {}

// senderPredicate selects the messages (alias m.) sender sent, with bind parameters numbered from first.
func senderPredicate(sender Sender, first int) (string, []any) {
	switch sender := sender.(type) {
	case AccountSender:
		return fmt.Sprintf("m.sender_namespace = $%d AND m.sender_name = $%d AND m.sender_uid = $%d", first, first+1, first+2),
			[]any{sender.Account.Namespace, sender.Account.Name, sender.UID}
	case IdentitySender:
		return fmt.Sprintf("m.sender_namespace = $%d AND m.sender_name = $%d AND m.sender_identity_name = $%d AND m.sender_identity_uid = $%d",
				first, first+1, first+2, first+3),
			[]any{sender.Sandbox.Namespace, sender.Sandbox.Name, sender.Identity.Name, sender.Identity.UID}
	default:
		panic(fmt.Sprintf("unknown message sender %T", sender))
	}
}

type messagePayload struct {
	Body        string  `json:"body"`
	FromSession *string `json:"from_session,omitempty"`
}

// Validate checks the bounds of the message, its body against limit UTF-8 bytes. The destination's UID may be
// empty, for the Sandbox of that name as it is now.
func (m Message) Validate(limit int) error {
	if err := errors.Join(
		model.CheckText("destination_ref.namespace", m.DestinationRef.Namespace, 1, 63),
		model.CheckText("destination_ref.name", m.DestinationRef.Name, 1, 63),
		model.CheckText("destination_ref.uid", m.DestinationRef.UID, 0, 128),
		model.CheckText("session_id", m.SessionID, 1, 200),
		model.CheckText("idempotency_key", m.IdempotencyKey, 1, 200),
	); err != nil {
		return err
	}
	if m.FromSession != nil {
		if err := model.CheckText("from_session", *m.FromSession, 1, 200); err != nil {
			return err
		}
	}
	if !utf8.ValidString(m.Body) {
		return model.Invalid("body is not valid UTF-8")
	}
	if len(m.Body) < 1 || len(m.Body) > limit {
		return model.Invalid("body must be 1 to %d UTF-8 bytes", limit)
	}
	return nil
}

// payload is the entry's payload: what the sender wrote.
func (m Message) payload() ([]byte, error) {
	return json.Marshal(messagePayload{Body: m.Body, FromSession: m.FromSession})
}

// SentMessage is a message as its sender sees it: where it went and how far the recipient's inbox has moved
// past it, never the inbox's cursors or other entries.
type SentMessage struct {
	ID             uuid.UUID
	DestinationRef model.DestinationRef
	SessionID      string
	IdempotencyKey string
	AcceptedAt     time.Time
	// Covered is whether a notice has named the message to the recipient Session.
	Covered bool
	// Acknowledged is whether the recipient acknowledged its inbox through the message.
	Acknowledged bool
	// InboxRetired is whether the recipient's inbox was retired: a message no notice covers then never gets one.
	InboxRetired bool
}

// OpenInbox creates the recipient's inbox for a Session if it has none, within the recipient's inbox quota. The
// caller has confirmed that the Session runs in the destination Sandbox.
func (s *Store) OpenInbox(ctx context.Context, recipient Owner, ref model.DestinationRef, sessionID string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		_, err := s.ensureInbox(ctx, tx, recipient, ref, sessionID)
		return err
	})
}

// Send appends the message to the recipient's inbox for its Session at the next cursor, and makes the inbox due
// for a notice. A retry under the same key answers with the message already sent if its body and label match,
// and fails with ErrIdempotencyKeyReused if not. It fails with ErrNoInbox when the Session has no inbox, with
// ErrInboxRetired for a retired one, and with a *QuotaError when the sender already has the most unacknowledged
// messages there.
func (s *Store) Send(ctx context.Context, message Message) (SentMessage, error) {
	payload, err := message.payload()
	if err != nil {
		return SentMessage{}, err
	}
	var named model.ServiceAccount
	var senderIdentity *model.Identity
	var senderUID, identityName, identityUID sql.NullString
	// unacknowledged counts the sender's messages; an AccountSender's include those a release before
	// sender UIDs recorded none, never an IdentitySender's.
	var unacknowledged string
	switch sender := message.Sender.(type) {
	case AccountSender:
		if sender.UID == "" {
			return SentMessage{}, ErrNotFound
		}
		named, senderUID = sender.Account, sql.NullString{String: sender.UID, Valid: true}
		unacknowledged = `(sender_uid = $5 OR sender_uid IS NULL AND sender_identity_uid IS NULL)`
	case IdentitySender:
		if sender.Identity.Name == "" || sender.Identity.UID == "" || sender.Sandbox.Name == "" {
			return SentMessage{}, ErrNotFound
		}
		named, senderIdentity = sender.Sandbox, &sender.Identity
		identityName = sql.NullString{String: sender.Identity.Name, Valid: true}
		identityUID = sql.NullString{String: sender.Identity.UID, Valid: true}
		unacknowledged = `sender_identity_uid = $5`
	default:
		panic(fmt.Sprintf("unknown message sender %T", sender))
	}
	if owner, ok := message.Recipient.(AccountOwner); ok && owner.AccountUID == "" {
		return SentMessage{}, ErrNotFound
	}
	digest := sha256.Sum256(payload)
	var sent SentMessage
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		predicate, args := ownerPredicate(message.Recipient, "", 2)
		inbox, err := scanInbox(tx.QueryRowContext(ctx,
			`SELECT `+inboxColumns+` FROM inbox WHERE destination_key = $1 AND `+predicate+` FOR UPDATE`,
			append([]any{model.DestinationKey(message.DestinationRef, message.SessionID)}, args...)...))
		if errors.Is(err, sql.ErrNoRows) {
			return ErrNoInbox
		}
		if err != nil {
			return err
		}
		var id uuid.UUID
		var existing []byte
		var establishedUID, establishedIdentityName, establishedIdentityUID sql.NullString
		err = tx.QueryRowContext(ctx, `
			SELECT id, digest, sender_uid, sender_identity_name, sender_identity_uid FROM message
			WHERE inbox_id = $1 AND sender_namespace = $2 AND sender_name = $3 AND idempotency_key = $4`,
			inbox.ID, named.Namespace, named.Name, message.IdempotencyKey).
			Scan(&id, &existing, &establishedUID, &establishedIdentityName, &establishedIdentityUID)
		sameSender := establishedUID.Valid && establishedUID == senderUID ||
			establishedIdentityUID.Valid && establishedIdentityName == identityName && establishedIdentityUID == identityUID
		switch {
		case err == nil && !sameSender:
			return ErrNotFound
		case err == nil && !bytes.Equal(existing, digest[:]):
			return ErrIdempotencyKeyReused
		case err == nil:
			sent, err = sentMessage(ctx, tx, message.Sender, id)
			return err
		case !errors.Is(err, sql.ErrNoRows):
			return err
		}
		if inbox.Retired {
			return ErrInboxRetired
		}
		var pending int
		sender := senderUID
		if identityUID.Valid {
			sender = identityUID
		}
		if err := tx.QueryRowContext(ctx, `
			SELECT count(*) FROM message
			WHERE inbox_id = $1 AND sender_namespace = $2 AND sender_name = $3 AND cursor > $4 AND `+unacknowledged,
			inbox.ID, named.Namespace, named.Name, inbox.Acknowledged, sender).Scan(&pending); err != nil {
			return err
		}
		if pending >= s.quotas.UnacknowledgedMessagesPerSender {
			return &QuotaError{Quota: UnacknowledgedMessagesPerSender, Limit: s.quotas.UnacknowledgedMessagesPerSender}
		}
		id = uuid.New()
		event := model.MessageEvent{MessageID: id, Sender: named, SenderIdentity: senderIdentity}
		accepted, cursor := now(), inbox.LastCursor+1
		// A worker's release works out when the notice is due, so the inbox is only made due now.
		if _, err := tx.ExecContext(ctx,
			`UPDATE inbox SET last_cursor = $2, next_attempt = LEAST(COALESCE(next_attempt, $3), $3) WHERE id = $1`,
			inbox.ID, cursor, accepted); err != nil {
			return err
		}
		if err := entrystorage.Insert(ctx, tx, inbox.ID, cursor, event, json.RawMessage(payload), accepted); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO message (id, inbox_id, cursor, sender_namespace, sender_name, idempotency_key, digest, sender_uid,
				sender_identity_name, sender_identity_uid)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
			id, inbox.ID, cursor, named.Namespace, named.Name, message.IdempotencyKey, digest[:], senderUID,
			identityName, identityUID); err != nil {
			return err
		}
		sent = SentMessage{
			ID: id, DestinationRef: inbox.DestinationRef, SessionID: inbox.SessionID, IdempotencyKey: message.IdempotencyKey,
			AcceptedAt: accepted,
		}
		return notify(ctx, tx)
	})
	return sent, err
}

// SentMessage returns a message the sender sent. Another sender's message, an unknown ID and a message whose
// inbox was purged are all ErrNotFound.
func (s *Store) SentMessage(ctx context.Context, sender Sender, id uuid.UUID) (SentMessage, error) {
	var sent SentMessage
	err := s.transaction(ctx, func(tx *sql.Tx) (err error) {
		sent, err = sentMessage(ctx, tx, sender, id)
		return err
	})
	return sent, err
}

func sentMessage(ctx context.Context, tx *sql.Tx, sender Sender, id uuid.UUID) (SentMessage, error) {
	sent := SentMessage{ID: id}
	var inboxID uuid.UUID
	var namespace, name, uid sql.NullString
	predicate, args := senderPredicate(sender, 2)
	err := tx.QueryRowContext(ctx, `
		SELECT i.id, i.destination_namespace, i.destination_name, i.destination_uid,
			i.session_id, m.idempotency_key, e.created_at,
			m.cursor <= i.covered, m.cursor <= i.acknowledged, i.retired
		FROM message m JOIN inbox i ON i.id = m.inbox_id JOIN entry e ON e.inbox_id = m.inbox_id AND e.cursor = m.cursor
		WHERE m.id = $1 AND `+predicate,
		append([]any{id}, args...)...).Scan(&inboxID, &namespace, &name, &uid,
		&sent.SessionID, &sent.IdempotencyKey, &sent.AcceptedAt,
		&sent.Covered, &sent.Acknowledged, &sent.InboxRetired)
	if errors.Is(err, sql.ErrNoRows) {
		return SentMessage{}, ErrNotFound
	}
	if err != nil {
		return SentMessage{}, err
	}
	sent.AcceptedAt = sent.AcceptedAt.UTC()
	sent.DestinationRef, err = destinationRefFromColumns(inboxID, namespace, name, uid)
	if err != nil {
		return SentMessage{}, err
	}
	return sent, nil
}

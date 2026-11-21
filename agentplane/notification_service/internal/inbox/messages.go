package inbox

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"time"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"

	entrystorage "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox/internal/entry"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// MessageBodyCeiling is the most UTF-8 bytes a message body can hold: the database refuses more, and the
// service's configured limit is at most this.
const MessageBodyCeiling = 256 * 1024

// ErrNoInbox is a message to a Session whose inbox does not exist yet. OpenInbox creates it after the caller has
// authorized the recipient Session.
var ErrNoInbox = errors.New("the Session has no inbox yet")

// Message is a message a workload sends to a Session's inbox.
type Message struct {
	Sender IdentitySender
	// Recipient is the exact Identity incarnation the inbox belongs to.
	Recipient Owner
	SessionID string
	// IdempotencyKey is unique per sender and inbox: an identical retry answers with the message it sent.
	IdempotencyKey string
	Body           string
	// FromSession is the sender's own label for the Session it writes from, which nothing verifies.
	FromSession *string
}

// IdentitySender is an Identity incarnation sending from the Sandbox its workload credential is
// controlled by. Recipients see the message as from the Identity and that Sandbox's name, the
// ServiceAccount the Sandbox ran as before it ran as an Identity.
type IdentitySender struct {
	Identity model.Identity
	Sandbox  model.ServiceAccount
}

type messagePayload struct {
	Body        string  `json:"body"`
	FromSession *string `json:"from_session,omitempty"`
}

// Validate checks the bounds of the message, its body against limit UTF-8 bytes.
func (m Message) Validate(limit int) error {
	if err := errors.Join(
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

// OpenInbox creates the recipient's inbox for a Session if it has none, within the recipient Identity's inbox quota.
func (s *Store) OpenInbox(ctx context.Context, recipient Owner, sessionID string) error {
	return s.transaction(ctx, func(tx transaction.Tx) error {
		_, err := s.ensureInbox(ctx, tx, recipient, sessionID)
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
	sender := message.Sender
	if sender.Identity.Name == "" || sender.Identity.UID == "" || sender.Sandbox.Name == "" {
		return SentMessage{}, ErrNotFound
	}
	if message.Recipient.Identity.Name == "" || message.Recipient.Identity.UID == "" {
		return SentMessage{}, ErrNotFound
	}
	digest := sha256.Sum256(payload)
	var sent SentMessage
	err = s.transaction(ctx, func(tx transaction.Tx) error {
		predicate, args := ownerPredicate(message.Recipient, "", 2)
		inbox, err := scanInbox(tx.QueryRowContext(ctx,
			`SELECT `+inboxColumns+` FROM inbox WHERE session_id = $1 AND `+predicate+` FOR UPDATE`,
			append([]any{message.SessionID}, args...)...))
		if errors.Is(err, sql.ErrNoRows) {
			return ErrNoInbox
		}
		if err != nil {
			return err
		}
		var id uuid.UUID
		var existing []byte
		var establishedIdentityName, establishedIdentityUID sql.NullString
		err = tx.QueryRowContext(ctx, `
			SELECT id, digest, sender_identity_name, sender_identity_uid FROM message
			WHERE inbox_id = $1 AND sender_namespace = $2 AND sender_name = $3 AND idempotency_key = $4`,
			inbox.ID, sender.Sandbox.Namespace, sender.Sandbox.Name, message.IdempotencyKey).
			Scan(&id, &existing, &establishedIdentityName, &establishedIdentityUID)
		// A message stored without an Identity was sent before Identity senders; no sender retries it.
		sameSender := establishedIdentityUID.Valid &&
			establishedIdentityName.String == sender.Identity.Name && establishedIdentityUID.String == sender.Identity.UID
		switch {
		case err == nil && !sameSender:
			return ErrNotFound
		case err == nil && !bytes.Equal(existing, digest[:]):
			return ErrIdempotencyKeyReused
		case err == nil:
			sent, err = sentMessage(ctx, tx, sender, id)
			return err
		case !errors.Is(err, sql.ErrNoRows):
			return err
		}
		if inbox.Retired {
			return ErrInboxRetired
		}
		var pending int
		if err := tx.QueryRowContext(ctx, `
			SELECT count(*) FROM message
			WHERE inbox_id = $1 AND sender_namespace = $2 AND sender_name = $3 AND cursor > $4 AND sender_identity_uid = $5`,
			inbox.ID, sender.Sandbox.Namespace, sender.Sandbox.Name, inbox.Acknowledged, sender.Identity.UID).Scan(&pending); err != nil {
			return err
		}
		if pending >= s.quotas.UnacknowledgedMessagesPerSender {
			return &QuotaError{Quota: UnacknowledgedMessagesPerSender, Limit: s.quotas.UnacknowledgedMessagesPerSender}
		}
		id = uuid.New()
		event := sourceadapter.WrapMessageEvent(model.MessageEvent{MessageID: id, Sender: sender.Sandbox, SenderIdentity: &sender.Identity})
		accepted, cursor := now(), inbox.LastCursor+1
		// A worker's release works out when the notice is due, so the inbox is only made due now. Preserve an
		// existing failure deadline: a new entry must not turn a known delivery failure into a tight retry loop.
		if _, err := tx.ExecContext(ctx,
			`UPDATE inbox SET last_cursor = $2, next_attempt = CASE
				WHEN delivery_error IS NOT NULL AND next_attempt IS NOT NULL THEN next_attempt
				ELSE LEAST(COALESCE(next_attempt, $3), $3)
			END WHERE id = $1`,
			inbox.ID, cursor, accepted); err != nil {
			return err
		}
		input, err := sourceadapter.AppendInputFor(event, json.RawMessage(payload))
		if err != nil {
			return err
		}
		if err := entrystorage.Insert(ctx, tx, inbox.ID, cursor, input, accepted); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO message (id, inbox_id, cursor, sender_namespace, sender_name, idempotency_key, digest,
				sender_identity_name, sender_identity_uid)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
			id, inbox.ID, cursor, sender.Sandbox.Namespace, sender.Sandbox.Name, message.IdempotencyKey, digest[:],
			sender.Identity.Name, sender.Identity.UID); err != nil {
			return err
		}
		sent = SentMessage{
			ID: id, SessionID: inbox.SessionID, IdempotencyKey: message.IdempotencyKey, AcceptedAt: accepted,
		}
		return notify(ctx, tx)
	})
	return sent, err
}

// SentMessage returns a message the sender sent. Another sender's message, an unknown ID and a message whose
// inbox was purged are all ErrNotFound.
func (s *Store) SentMessage(ctx context.Context, sender IdentitySender, id uuid.UUID) (SentMessage, error) {
	var sent SentMessage
	err := s.transaction(ctx, func(tx transaction.Tx) (err error) {
		sent, err = sentMessage(ctx, tx, sender, id)
		return err
	})
	return sent, err
}

func sentMessage(ctx context.Context, tx transaction.Tx, sender IdentitySender, id uuid.UUID) (SentMessage, error) {
	sent := SentMessage{ID: id}
	err := tx.QueryRowContext(ctx, `
		SELECT i.session_id, m.idempotency_key, e.created_at,
			m.cursor <= i.covered, m.cursor <= i.acknowledged, i.retired
		FROM message m JOIN inbox i ON i.id = m.inbox_id JOIN entry e ON e.inbox_id = m.inbox_id AND e.cursor = m.cursor
		WHERE m.id = $1 AND m.sender_namespace = $2 AND m.sender_name = $3
			AND m.sender_identity_name = $4 AND m.sender_identity_uid = $5`,
		id, sender.Sandbox.Namespace, sender.Sandbox.Name, sender.Identity.Name, sender.Identity.UID).Scan(&sent.SessionID,
		&sent.IdempotencyKey, &sent.AcceptedAt, &sent.Covered, &sent.Acknowledged, &sent.InboxRetired)
	if errors.Is(err, sql.ErrNoRows) {
		return SentMessage{}, ErrNotFound
	}
	if err != nil {
		return SentMessage{}, err
	}
	sent.AcceptedAt = sent.AcceptedAt.UTC()
	return sent, nil
}

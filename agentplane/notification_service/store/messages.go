package store

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"time"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
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
	Sender    model.ServiceAccount
	SenderUID string
	// Recipient owns the inbox: the model.ServiceAccount the destination Sandbox runs as.
	Recipient      model.ServiceAccount
	RecipientUID   string
	DestinationRef model.DestinationRef
	SessionID      string
	// IdempotencyKey is unique per sender and inbox: an identical retry answers with the message it sent.
	IdempotencyKey string
	Body           string
	// FromSession is the sender's own label for the Session it writes from, which nothing verifies.
	FromSession *string
}

type messagePayload struct {
	Body        string  `json:"body"`
	FromSession *string `json:"from_session,omitempty"`
}

func encodeMessagePayload(body string, fromSession *string) ([]byte, error) {
	return json.Marshal(messagePayload{Body: body, FromSession: fromSession})
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
	return encodeMessagePayload(m.Body, m.FromSession)
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
func (s *Store) OpenInbox(ctx context.Context, recipient model.ServiceAccount, ref model.DestinationRef, sessionID, ownerUID string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		_, err := s.ensureInbox(ctx, tx, AccountOwner{Account: recipient, AccountUID: ownerUID, SandboxUID: ref.UID}, ref, sessionID)
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
	if message.SenderUID == "" || message.RecipientUID == "" {
		return SentMessage{}, ErrNotFound
	}
	digest := sha256.Sum256(payload)
	var sent SentMessage
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		inbox, err := scanInbox(tx.QueryRowContext(ctx,
			`SELECT `+inboxColumns+` FROM inbox WHERE owner_namespace = $1 AND owner_name = $2 AND destination_key = $3 AND owner_uid = $4 FOR UPDATE`,
			message.Recipient.Namespace, message.Recipient.Name, model.DestinationKey(message.DestinationRef, message.SessionID), message.RecipientUID))
		if errors.Is(err, sql.ErrNoRows) {
			return ErrNoInbox
		}
		if err != nil {
			return err
		}
		var id uuid.UUID
		var existing []byte
		var establishedSender sql.NullString
		err = tx.QueryRowContext(ctx, `
			SELECT id, digest, sender_uid FROM message
			WHERE inbox_id = $1 AND sender_namespace = $2 AND sender_name = $3 AND idempotency_key = $4`,
			inbox.ID, message.Sender.Namespace, message.Sender.Name, message.IdempotencyKey).Scan(&id, &existing, &establishedSender)
		switch {
		case err == nil && (!establishedSender.Valid || establishedSender.String != message.SenderUID):
			return ErrNotFound
		case err == nil && !bytes.Equal(existing, digest[:]):
			return ErrIdempotencyKeyReused
		case err == nil:
			sent, err = sentMessage(ctx, tx, message.Sender, id, message.SenderUID)
			return err
		case !errors.Is(err, sql.ErrNoRows):
			return err
		}
		if inbox.Retired {
			return ErrInboxRetired
		}
		var unacknowledged int
		if err := tx.QueryRowContext(ctx, `
			SELECT count(*) FROM message
			WHERE inbox_id = $1 AND sender_namespace = $2 AND sender_name = $3 AND cursor > $4 AND (sender_uid = $5 OR sender_uid IS NULL)`,
			inbox.ID, message.Sender.Namespace, message.Sender.Name, inbox.Acknowledged, message.SenderUID).Scan(&unacknowledged); err != nil {
			return err
		}
		if unacknowledged >= s.quotas.UnacknowledgedMessagesPerSender {
			return &QuotaError{Quota: UnacknowledgedMessagesPerSender, Limit: s.quotas.UnacknowledgedMessagesPerSender}
		}
		id = uuid.New()
		event := model.MessageEvent{MessageID: id, Sender: message.Sender}
		accepted, cursor := now(), inbox.LastCursor+1
		// A worker's release works out when the notice is due, so the inbox is only made due now.
		if _, err := tx.ExecContext(ctx,
			`UPDATE inbox SET last_cursor = $2, next_attempt = LEAST(COALESCE(next_attempt, $3), $3) WHERE id = $1`,
			inbox.ID, cursor, accepted); err != nil {
			return err
		}
		if err := insertEntry(ctx, tx, inbox.ID, cursor, event, json.RawMessage(payload), accepted); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO message (id, inbox_id, cursor, sender_namespace, sender_name, idempotency_key, digest, sender_uid)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
			id, inbox.ID, cursor, message.Sender.Namespace, message.Sender.Name, message.IdempotencyKey, digest[:], message.SenderUID); err != nil {
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
func (s *Store) SentMessage(ctx context.Context, sender model.ServiceAccount, id uuid.UUID, senderUID string) (SentMessage, error) {
	var sent SentMessage
	err := s.transaction(ctx, func(tx *sql.Tx) (err error) {
		sent, err = sentMessage(ctx, tx, sender, id, senderUID)
		return err
	})
	return sent, err
}

func sentMessage(ctx context.Context, tx *sql.Tx, sender model.ServiceAccount, id uuid.UUID, senderUID string) (SentMessage, error) {
	sent := SentMessage{ID: id}
	var inboxID uuid.UUID
	var namespace, name, uid sql.NullString
	err := tx.QueryRowContext(ctx, `
		SELECT i.id, i.destination_namespace, i.destination_name, i.destination_uid,
			i.session_id, m.idempotency_key, e.created_at,
			m.cursor <= i.covered, m.cursor <= i.acknowledged, i.retired
		FROM message m JOIN inbox i ON i.id = m.inbox_id JOIN entry e ON e.inbox_id = m.inbox_id AND e.cursor = m.cursor
		WHERE m.id = $1 AND m.sender_namespace = $2 AND m.sender_name = $3 AND m.sender_uid = $4`,
		id, sender.Namespace, sender.Name, senderUID).Scan(&inboxID, &namespace, &name, &uid,
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

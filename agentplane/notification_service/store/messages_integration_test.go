package store

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// messageQuotas allow two unacknowledged messages per sender.
func messageQuotas() Quotas {
	quotas := defaultQuotas()
	quotas.UnacknowledgedMessagesPerSender = 2
	return quotas
}

// message is a message from sender to the fixture owner's Sandbox, for the Session body names.
func (f fixture) message(sender model.ServiceAccount, key, text string) Message {
	creation := body(key)
	return Message{
		Sender: sender, Recipient: f.owner, DestinationRef: creation.DestinationRef, SessionID: creation.SessionID,
		IdempotencyKey: key, Body: text,
	}
}

func (f fixture) openInbox(t *testing.T) {
	t.Helper()
	creation := body("unused")
	if err := f.OpenInbox(t.Context(), f.owner, creation.DestinationRef, creation.SessionID); err != nil {
		t.Fatal(err)
	}
}

func (f fixture) send(t *testing.T, message Message) SentMessage {
	t.Helper()
	sent, err := f.Send(t.Context(), message)
	if err != nil {
		t.Fatal(err)
	}
	return sent
}

func (f fixture) sentMessage(t *testing.T, sender model.ServiceAccount, id uuid.UUID) SentMessage {
	t.Helper()
	sent, err := f.SentMessage(t.Context(), sender, id)
	if err != nil {
		t.Fatal(err)
	}
	return sent
}

func (f fixture) recipientInbox(t *testing.T) Inbox {
	t.Helper()
	inboxes, err := f.ListInboxes(t.Context(), f.incarnation())
	if err != nil {
		t.Fatal(err)
	}
	if len(inboxes) != 1 {
		t.Fatalf("%d inboxes, want 1", len(inboxes))
	}
	return inboxes[0]
}

func TestAMessageIsAnEntryOfItsSessionsInboxThatItsSenderFollows(t *testing.T) {
	f := newClaimFixture(t)
	f.Store = New(f.db, messageQuotas())
	sender := newOwner()
	message := f.message(sender, "first", "Please review the change.")
	message.FromSession = ptr("test-from-session")
	if _, err := f.Send(t.Context(), message); !errors.Is(err, ErrNoInbox) {
		t.Fatalf("error = %v before the inbox exists, want ErrNoInbox", err)
	}
	f.openInbox(t)
	sent := f.send(t, message)
	if sent.DestinationRef != message.DestinationRef || sent.SessionID != message.SessionID || sent.IdempotencyKey != "first" ||
		sent.Covered || sent.Acknowledged || sent.InboxRetired {
		t.Fatalf("sent = %+v", sent)
	}

	inbox := f.recipientInbox(t)
	page := f.read(t, inbox.ID, 0)
	if len(page.Entries) != 1 {
		t.Fatalf("%d entries, want 1", len(page.Entries))
	}
	entry := page.Entries[0]
	if entry.Cursor != 1 || entry.Event != model.EventIdentity(model.MessageEvent{MessageID: sent.ID, Sender: sender}) || entry.SubscriptionIDs != nil {
		t.Fatalf("entry = %+v", entry)
	}
	sameJSON(t, entry.Payload, []byte(`{"body": "Please review the change.", "from_session": "test-from-session"}`))
	var legacyPayloadNull, typedPayloadPresent bool
	if err := f.db.QueryRowContext(t.Context(), `SELECT payload IS NULL, typed_payload IS NOT NULL
		FROM entry WHERE message_id = $1`, sent.ID).Scan(&legacyPayloadNull, &typedPayloadPresent); err != nil {
		t.Fatal(err)
	}
	if !legacyPayloadNull || typedPayloadPresent {
		t.Fatalf("message storage has legacy-payload-null=%t typed-payload-present=%t; want canonical text without legacy JSON or protobuf",
			legacyPayloadNull, typedPayloadPresent)
	}
	var storedBody, storedFromSession sql.NullString
	if err := f.db.QueryRowContext(t.Context(), `SELECT message_body, message_from_session FROM entry
		WHERE inbox_id = $1 AND cursor = $2`, inbox.ID, entry.Cursor).Scan(&storedBody, &storedFromSession); err != nil {
		t.Fatal(err)
	}
	if !storedBody.Valid || storedBody.String != message.Body || !storedFromSession.Valid || storedFromSession.String != *message.FromSession {
		t.Fatalf("stored message text = (%q, %q), want (%q, %q)", storedBody.String, storedFromSession.String,
			message.Body, *message.FromSession)
	}

	// The message made the inbox due, and a notice covers it.
	claim := f.claim(t)
	if claim.ID != inbox.ID {
		t.Fatalf("claimed %s, want the recipient's inbox %s", claim.ID, inbox.ID)
	}
	f.prepareNotice(t, claim)
	if got := f.sentMessage(t, sender, sent.ID); !got.Covered || got.Acknowledged || !got.AcceptedAt.Equal(sent.AcceptedAt) {
		t.Fatalf("after the notice: %+v", got)
	}
	f.acknowledge(t, inbox.ID, 1)
	if got := f.sentMessage(t, sender, sent.ID); !got.Acknowledged {
		t.Fatalf("after the acknowledgement: %+v", got)
	}

	for name, lookup := range map[string]struct {
		sender model.ServiceAccount
		id     uuid.UUID
	}{"the recipient": {f.owner, sent.ID}, "another account": {newOwner(), sent.ID}, "an unknown ID": {sender, uuid.New()}} {
		if _, err := f.SentMessage(t.Context(), lookup.sender, lookup.id); !errors.Is(err, ErrNotFound) {
			t.Errorf("%s: error = %v, want ErrNotFound", name, err)
		}
	}
}

func TestARetiredInboxTakesNoNewMessageAndItsPurgeEndsTheReceipts(t *testing.T) {
	f := newFixture(t, messageQuotas())
	sender := newOwner()
	f.openInbox(t)
	message := f.message(sender, "first", "test body")
	sent := f.send(t, message)
	inbox := f.recipientInbox(t)
	if err := f.Retire(t.Context(), f.incarnation(), inbox.ID); err != nil {
		t.Fatal(err)
	}
	if got := f.sentMessage(t, sender, sent.ID); !got.InboxRetired || got.Covered {
		t.Fatalf("after retirement: %+v", got)
	}
	if _, err := f.Send(t.Context(), f.message(sender, "second", "test body")); !errors.Is(err, ErrInboxRetired) {
		t.Fatalf("error = %v, want ErrInboxRetired", err)
	}
	// A retry is answered with what was accepted before.
	if retried := f.send(t, message); retried.ID != sent.ID || !retried.InboxRetired {
		t.Fatalf("retry = %+v", retried)
	}

	f.exec(t, `UPDATE inbox SET updated_at = now() - interval '31 days' WHERE id = $1`, inbox.ID)
	f.cleanAll(t)
	if _, err := f.SentMessage(t.Context(), sender, sent.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("error = %v after the purge, want ErrNotFound", err)
	}
}

func TestARetryAnswersWithTheMessageAndAKeyReusedForOtherContentIsRefused(t *testing.T) {
	f := newFixture(t, messageQuotas())
	sender := newOwner()
	f.openInbox(t)
	message := f.message(sender, "first", "test body")
	sent := f.send(t, message)
	// The payload expires; the retry is still recognized.
	f.exec(t, `UPDATE entry SET payload = NULL, typed_payload = NULL, message_body = NULL, message_from_session = NULL
		WHERE inbox_id = $1`, f.recipientInbox(t).ID)
	if retried := f.send(t, message); retried.ID != sent.ID || !retried.AcceptedAt.Equal(sent.AcceptedAt) {
		t.Fatalf("retry = %+v, want %+v", retried, sent)
	}
	otherBody, otherLabel := message, message
	otherBody.Body = "another body"
	otherLabel.FromSession = ptr("test-from-session")
	for name, reused := range map[string]Message{"body": otherBody, "from_session": otherLabel} {
		if _, err := f.Send(t.Context(), reused); !errors.Is(err, ErrIdempotencyKeyReused) {
			t.Errorf("another %s: error = %v, want ErrIdempotencyKeyReused", name, err)
		}
	}
	// The key is the sender's own: another sender's message under it is another message.
	if other := f.send(t, f.message(newOwner(), "first", "test body")); other.ID == sent.ID {
		t.Fatal("another sender's message was answered with the first sender's")
	}
	if got := f.recipientInbox(t).LastCursor; got != 2 {
		t.Fatalf("last cursor = %d, want 2", got)
	}
}

func TestASenderMayHaveOnlySoManyUnacknowledgedMessagesInAnInbox(t *testing.T) {
	f := newFixture(t, messageQuotas())
	sender := newOwner()
	f.openInbox(t)
	f.send(t, f.message(sender, "first", "test body"))
	f.send(t, f.message(sender, "second", "test body"))
	_, err := f.Send(t.Context(), f.message(sender, "third", "test body"))
	requireQuota(t, err, UnacknowledgedMessagesPerSender, 2)
	// Each sender has its own allowance.
	f.send(t, f.message(newOwner(), "first", "test body"))
	f.acknowledge(t, f.recipientInbox(t).ID, 1)
	f.send(t, f.message(sender, "third", "test body"))
}

func TestMessagesDoNotCountTowardsTheInboxsEntryLimit(t *testing.T) {
	quotas := messageQuotas()
	quotas.EntriesPerInbox = 1
	f := newFixture(t, quotas)
	subscription := f.subscribe(t, body("subscription"))
	sender := newOwner()
	f.send(t, f.message(sender, "first", "test body"))
	f.send(t, f.message(sender, "second", "test body"))
	f.addEvents(t, subscription, 1, 1)
	requireQuota(t, f.appendEvent(subscription.InboxID, subscription.ID,
		model.ActionsEvent{RequestID: subscription.Source.(model.ActionsSource).RequestID, Sequence: 2}, payloadOf(model.ActionsEvent{Sequence: 2})), EntriesPerInbox, 1)
}

func TestTheDatabaseHoldsNoBodyBeyondTheCeiling(t *testing.T) {
	f := newFixture(t, messageQuotas())
	f.openInbox(t)
	body := strings.Repeat("é", MessageBodyCeiling/2)
	sender := newOwner()
	sent := f.send(t, f.message(sender, "at-ceiling", body))
	inbox := f.recipientInbox(t)
	var storedBody string
	var storedFromSession sql.NullString
	if err := f.db.QueryRowContext(t.Context(), `SELECT message_body, message_from_session FROM entry
		WHERE inbox_id = $1 AND cursor = 1`, inbox.ID).Scan(&storedBody, &storedFromSession); err != nil {
		t.Fatal(err)
	}
	if storedBody != body || storedFromSession.Valid {
		t.Fatalf("stored message text has %d bytes, sender label = %+v; want exact %d-byte body and SQL NULL label",
			len(storedBody), storedFromSession, len(body))
	}
	_, err := f.db.ExecContext(t.Context(), `UPDATE entry SET message_body = $3 WHERE inbox_id = $1 AND cursor = $2`,
		inbox.ID, 1, body+"x")
	if refused := (*pq.Error)(nil); !errors.As(err, &refused) || refused.Constraint != "entry_message_text_body_ceiling" {
		t.Fatalf("text body update error = %v, want the canonical text ceiling's constraint", err)
	}
	_, err = f.Send(t.Context(), f.message(sender, "beyond-ceiling", body+"x"))
	if refused := (*pq.Error)(nil); !errors.As(err, &refused) || refused.Constraint != "entry_message_text_body_ceiling" {
		t.Fatalf("oversized Message write error = %v, want the canonical text ceiling's constraint", err)
	}
	if got := f.sentMessage(t, sender, sent.ID); got.ID != sent.ID {
		t.Fatalf("sender receipt changed while checking text ceiling: %+v", got)
	}
}

func TestMessageTextChecksMatchProviderAndSenderShape(t *testing.T) {
	f := newFixture(t, messageQuotas())
	f.openInbox(t)
	sender := newOwner()
	sent := f.send(t, f.message(sender, "shape", "body"))
	inbox := f.recipientInbox(t)
	_, err := f.db.ExecContext(t.Context(), `UPDATE entry SET message_body = NULL, message_from_session = 'label'
		WHERE inbox_id = $1 AND cursor = 1`, inbox.ID)
	requireConstraint(t, err, "23514", "entry_message_text_shape")
	_, err = f.db.ExecContext(t.Context(), `UPDATE entry SET message_from_session = $3 WHERE inbox_id = $1 AND cursor = $2`,
		inbox.ID, 1, strings.Repeat("s", 201))
	requireConstraint(t, err, "23514", "entry_message_text_sender_length")

	creation := body("non-message-text-shape")
	subscription := f.subscribe(t, creation)
	event := model.ActionsEvent{RequestID: creation.Source.(model.ActionsSource).RequestID, Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	var actionCursor int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT cursor FROM entry
		WHERE inbox_id = $1 AND identity_provider = 'actions'`, subscription.InboxID).Scan(&actionCursor); err != nil {
		t.Fatal(err)
	}
	_, err = f.db.ExecContext(t.Context(), `UPDATE entry SET message_body = 'not a message'
		WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, actionCursor)
	requireConstraint(t, err, "23514", "entry_message_text_shape")
	if got := f.sentMessage(t, sender, sent.ID); got.ID != sent.ID {
		t.Fatalf("sender receipt changed during shape checks: %+v", got)
	}
}

func TestMessageExpiryClearsEveryBodyRepresentationAndKeepsTheDigestReceipt(t *testing.T) {
	f := newFixture(t, messageQuotas())
	sender := newOwner()
	f.openInbox(t)
	message := f.message(sender, "expired-message", "body that should expire")
	message.FromSession = ptr("sender-session")
	sent := f.send(t, message)
	inbox := f.recipientInbox(t)
	f.exec(t, `UPDATE entry SET created_at = now() - interval '31 days' WHERE inbox_id = $1 AND cursor = 1`, inbox.ID)
	f.cleanAll(t)
	var payload, typedPayload []byte
	var body, fromSession sql.NullString
	if err := f.db.QueryRowContext(t.Context(), `SELECT payload, typed_payload, message_body, message_from_session
		FROM entry WHERE inbox_id = $1 AND cursor = 1`, inbox.ID).Scan(&payload, &typedPayload, &body, &fromSession); err != nil {
		t.Fatal(err)
	}
	if payload != nil || typedPayload != nil || body.Valid || fromSession.Valid {
		t.Fatalf("expired content remains: JSON nil=%t protobuf nil=%t body=%+v sender=%+v", payload == nil,
			typedPayload == nil, body, fromSession)
	}
	if retried := f.send(t, message); retried.ID != sent.ID {
		t.Fatalf("retry after expiry = %+v, want original receipt %s", retried, sent.ID)
	}
}

func TestMessageTextExpansionPreservesPredecessorPayloads(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	if _, err := db.ExecContext(ctx, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatalf("reset cloned test database schema: %v", err)
	}
	if err := migrations.UpTo(ctx, db, 19); err != nil {
		t.Fatalf("apply migrations through predecessor Notification writer release: %v", err)
	}
	inboxID := uuid.New()
	if _, err := db.ExecContext(ctx, `INSERT INTO inbox (
		id, owner_namespace, owner_name, destination_key, destination_namespace, destination_name, destination_uid,
		session_id, last_cursor, acknowledged, covered, expired_through, retired, updated_at, claim_until)
		VALUES ($1, 'message-text-expansion', 'owner', 'stored-key', 'destination-ns', 'destination-name', 'uid',
		'session-before-expansion', 1, 0, 0, 0, false, now(), now())`, inboxID); err != nil {
		t.Fatalf("insert predecessor inbox: %v", err)
	}
	event := model.MessageEvent{MessageID: uuid.New(), Sender: model.ServiceAccount{Namespace: "sender-ns", Name: "sender-name"}}
	eventJSON, err := json.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}
	payload := json.RawMessage(`{"body":"retained message","from_session":"launch-session"}`)
	// Literal EntryContent.Message protobuf bytes from the predecessor writer prove expansion preserves retained data
	// without keeping the retired Message protobuf codec in production.
	legacyTypedPayload := []byte("\x1a\x22\x0a\x10retained message\x12\x0elaunch-session")
	if _, err := db.ExecContext(ctx, `INSERT INTO entry (
		inbox_id, cursor, event, payload, created_at, identity_provider, message_id,
		message_sender_namespace, message_sender_name, typed_payload)
		VALUES ($1, 1, $2::jsonb, $3::jsonb, now(), 'message', $4, $5, $6, $7)`,
		inboxID, string(eventJSON), string(payload), event.MessageID, event.Sender.Namespace, event.Sender.Name, legacyTypedPayload); err != nil {
		t.Fatalf("insert predecessor message entry: %v", err)
	}
	if err := migrations.UpTo(ctx, db, 20); err != nil {
		t.Fatalf("apply message text expansion: %v", err)
	}
	var payloadPreserved bool
	var gotTypedPayload []byte
	var messageBody, messageFromSession sql.NullString
	if err := db.QueryRowContext(ctx, `SELECT payload = $2::jsonb, typed_payload, message_body, message_from_session
		FROM entry WHERE inbox_id = $1 AND cursor = 1`, inboxID, string(payload)).Scan(
		&payloadPreserved, &gotTypedPayload, &messageBody, &messageFromSession,
	); err != nil {
		t.Fatalf("read predecessor message after expansion: %v", err)
	}
	if !payloadPreserved || !bytes.Equal(gotTypedPayload, legacyTypedPayload) || messageBody.Valid || messageFromSession.Valid {
		t.Fatalf("expansion changed predecessor representations: JSON preserved %t, protobuf equal %t, body %+v, sender %+v",
			payloadPreserved, bytes.Equal(gotTypedPayload, legacyTypedPayload), messageBody, messageFromSession)
	}
}

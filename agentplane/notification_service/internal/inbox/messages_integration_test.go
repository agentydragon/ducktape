package inbox

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"

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

// newSender is an Identity sending from a Sandbox that no other test's sender uses.
func newSender() IdentitySender {
	id := uuid.NewString()
	return IdentitySender{
		Identity: model.Identity{Name: "sender-identity-" + id, UID: "sender-uid-" + id},
		Sandbox:  model.ServiceAccount{Namespace: "test-namespace", Name: "sender-sandbox-" + id},
	}
}

// message is a message from sender to the fixture owner, for the Session body names.
func (f fixture) message(sender IdentitySender, key, text string) Message {
	return Message{
		Sender: sender, Recipient: f.incarnation(),
		SessionID: f.body(key).SessionID, IdempotencyKey: key, Body: text,
	}
}

func (f fixture) openInbox(t *testing.T) {
	t.Helper()
	creation := f.body("unused")
	if err := f.OpenInbox(t.Context(), f.incarnation(), creation.SessionID); err != nil {
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

func (f fixture) sentMessage(t *testing.T, sender IdentitySender, id uuid.UUID) SentMessage {
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
	f.Store = New(f.db, f.runner, messageQuotas())
	sender := newSender()
	message := f.message(sender, "first", "Please review the change.")
	message.FromSession = ptr("test-from-session")
	if _, err := f.Send(t.Context(), message); !errors.Is(err, ErrNoInbox) {
		t.Fatalf("error = %v before the inbox exists, want ErrNoInbox", err)
	}
	f.openInbox(t)
	sent := f.send(t, message)
	if sent.SessionID != message.SessionID || sent.IdempotencyKey != "first" ||
		sent.Covered || sent.Acknowledged || sent.InboxRetired {
		t.Fatalf("sent = %+v", sent)
	}

	inbox := f.recipientInbox(t)
	page := f.read(t, inbox.ID, 0)
	if len(page.Entries) != 1 {
		t.Fatalf("%d entries, want 1", len(page.Entries))
	}
	entry := page.Entries[0]
	event, ok := entry.Event.(sourceadapter.MessageEvent)
	if entry.Cursor != 1 || !ok || event.MessageID != sent.ID || event.Sender != sender.Sandbox ||
		event.SenderIdentity == nil || *event.SenderIdentity != sender.Identity || entry.SubscriptionIDs != nil {
		t.Fatalf("entry = %+v", entry)
	}
	sameJSON(t, entry.Payload, []byte(`{"body": "Please review the change.", "from_session": "test-from-session"}`))

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

	recipient := sender
	recipient.Identity = f.identity
	for name, lookup := range map[string]struct {
		sender IdentitySender
		id     uuid.UUID
	}{"the recipient": {recipient, sent.ID}, "another sender": {newSender(), sent.ID}, "an unknown ID": {sender, uuid.New()}} {
		if _, err := f.SentMessage(t.Context(), lookup.sender, lookup.id); !errors.Is(err, ErrNotFound) {
			t.Errorf("%s: error = %v, want ErrNotFound", name, err)
		}
	}
}

func TestNewMessageDoesNotShortenAnExistingDeliveryFailureRetry(t *testing.T) {
	f := newFixture(t, messageQuotas())
	f.openInbox(t)
	sender := newSender()
	f.send(t, f.message(sender, "first", "first entry"))
	inbox := f.recipientInbox(t)
	deadline := time.Now().Add(time.Hour).Truncate(time.Microsecond).UTC()
	if _, err := f.db.ExecContext(t.Context(), `
		UPDATE inbox SET delivery_error = 'DispatcherNotFoundError', next_attempt = $2 WHERE id = $1`,
		inbox.ID, deadline); err != nil {
		t.Fatal(err)
	}

	f.send(t, f.message(sender, "second", "second entry"))

	var next time.Time
	if err := f.db.QueryRowContext(t.Context(), `SELECT next_attempt FROM inbox WHERE id = $1`, inbox.ID).Scan(&next); err != nil {
		t.Fatal(err)
	}
	if !next.Equal(deadline) {
		t.Fatalf("next_attempt = %s, want preserved delivery retry %s", next, deadline)
	}
	if got := f.recipientInbox(t).LastCursor; got != 2 {
		t.Fatalf("last cursor = %d, want 2", got)
	}
}

func TestARetiredInboxTakesNoNewMessageAndItsPurgeEndsTheReceipts(t *testing.T) {
	f := newFixture(t, messageQuotas())
	sender := newSender()
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
	sender := newSender()
	f.openInbox(t)
	message := f.message(sender, "first", "test body")
	sent := f.send(t, message)
	// The payload expires; the retry is still recognized.
	f.exec(t, `UPDATE entry SET payload = NULL WHERE inbox_id = $1`, f.recipientInbox(t).ID)
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
	if other := f.send(t, f.message(newSender(), "first", "test body")); other.ID == sent.ID {
		t.Fatal("another sender's message was answered with the first sender's")
	}
	if got := f.recipientInbox(t).LastCursor; got != 2 {
		t.Fatalf("last cursor = %d, want 2", got)
	}
}

func TestASenderMayHaveOnlySoManyUnacknowledgedMessagesInAnInbox(t *testing.T) {
	f := newFixture(t, messageQuotas())
	sender := newSender()
	f.openInbox(t)
	f.send(t, f.message(sender, "first", "test body"))
	f.send(t, f.message(sender, "second", "test body"))
	_, err := f.Send(t.Context(), f.message(sender, "third", "test body"))
	requireQuota(t, err, UnacknowledgedMessagesPerSender, 2)
	// Each sender has its own allowance.
	f.send(t, f.message(newSender(), "first", "test body"))
	f.acknowledge(t, f.recipientInbox(t).ID, 1)
	f.send(t, f.message(sender, "third", "test body"))
}

func TestMessagesDoNotCountTowardsTheInboxsEntryLimit(t *testing.T) {
	quotas := messageQuotas()
	quotas.EntriesPerInbox = 1
	f := newFixture(t, quotas)
	subscription := f.subscribe(t, f.body("subscription"))
	sender := newSender()
	f.send(t, f.message(sender, "first", "test body"))
	f.send(t, f.message(sender, "second", "test body"))
	f.addEvents(t, subscription, 1, 1)
	requireQuota(t, f.appendEvent(subscription.InboxID, subscription.ID,
		sourceadapter.WrapActionsEvent(model.ActionsEvent{RequestID: subscription.Source.(sourceadapter.ActionsSource).RequestID, Sequence: 2}), payloadOf(sourceadapter.WrapActionsEvent(model.ActionsEvent{Sequence: 2}))), EntriesPerInbox, 1)
}

func TestTheDatabaseHoldsNoBodyBeyondTheCeiling(t *testing.T) {
	f := newFixture(t, messageQuotas())
	f.openInbox(t)
	f.send(t, f.message(newSender(), "at-ceiling", strings.Repeat("é", MessageBodyCeiling/2)))
	_, err := f.Send(t.Context(), f.message(newSender(), "beyond-ceiling", strings.Repeat("é", MessageBodyCeiling/2)+"x"))
	if refused := (*pq.Error)(nil); !errors.As(err, &refused) || refused.Constraint != "entry_message_body_bytes" {
		t.Fatalf("error = %v, want the native body ceiling's constraint", err)
	}
}

func TestMessageSenderIncarnationFencesReceiptsAndUncertainRetries(t *testing.T) {
	f := newFixture(t, messageQuotas())
	f.openInbox(t)
	sender := newSender()
	message := f.message(sender, "exact-sender", "one message")
	sent := f.send(t, message)
	replacement := sender
	replacement.Identity.UID = "replacement-sender-uid"
	if _, err := f.SentMessage(t.Context(), replacement, sent.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("replacement receipt = %v", err)
	}
	retry := message
	retry.Sender = replacement
	if _, err := f.Send(t.Context(), retry); !errors.Is(err, ErrNotFound) {
		t.Fatalf("replacement retry = %v", err)
	}
	// A message stored before Identity senders names no Identity, so no sender retries or follows it.
	f.exec(t, `UPDATE message SET sender_identity_name = NULL, sender_identity_uid = NULL WHERE id = $1`, sent.ID)
	if _, err := f.Send(t.Context(), message); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown original identity retry = %v", err)
	}
	if _, err := f.SentMessage(t.Context(), sender, sent.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown sender receipt = %v", err)
	}
}

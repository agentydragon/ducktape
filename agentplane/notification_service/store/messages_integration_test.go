package store

import (
	"errors"
	"strings"
	"testing"

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
	f.send(t, f.message(newOwner(), "at-ceiling", strings.Repeat("é", MessageBodyCeiling/2)))
	_, err := f.Send(t.Context(), f.message(newOwner(), "beyond-ceiling", strings.Repeat("é", MessageBodyCeiling/2)+"x"))
	if refused := (*pq.Error)(nil); !errors.As(err, &refused) || refused.Constraint != "entry_message_body_bytes" {
		t.Fatalf("error = %v, want the native body ceiling's constraint", err)
	}
}

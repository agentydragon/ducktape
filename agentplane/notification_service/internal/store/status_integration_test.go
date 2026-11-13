package store

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

var statusDebounce = Debounce{Quiet: time.Minute, MaxWait: 2 * time.Minute}

// uniqueSandbox is a destination no other test uses: operator reads cover every owner's inboxes.
func uniqueSandbox() model.DestinationRef {
	return model.DestinationRef{Namespace: "test-namespace", Name: "sandbox-" + uuid.NewString(), UID: "uid-" + uuid.NewString()}
}

func uniqueSession() string { return "session-" + uuid.NewString() }

func (f fixture) subscribeIn(t *testing.T, key, sessionID string, ref model.DestinationRef) Subscription {
	t.Helper()
	creation := sessionBody(key, sessionID)
	creation.DestinationRef = ref
	return f.subscribe(t, creation)
}

func (f fixture) onlyInbox(t *testing.T, ref model.DestinationRef) InboxStatus {
	t.Helper()
	var session string
	if err := f.db.QueryRowContext(t.Context(), `SELECT session_id FROM inbox WHERE destination_namespace=$1 AND destination_name=$2 AND destination_uid=$3`, ref.Namespace, ref.Name, ref.UID).Scan(&session); err != nil {
		t.Fatal(err)
	}
	inbox, _, err := f.SessionStatus(t.Context(), ref.Namespace, session, nil, statusDebounce, true, true)
	if err != nil || inbox == nil {
		t.Fatalf("SessionStatus = %v, %v", inbox, err)
	}
	return *inbox
}

// setNotice makes the notice of the inbox the one given, as the sender and the runner's receipts leave it.
func (f fixture) setNotice(t *testing.T, inboxID uuid.UUID, notice Notice) {
	t.Helper()
	f.exec(t, `
		INSERT INTO notice (inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, error, runner_cursor)
		VALUES ($1, $2, 1, 'notice', $3, $4, $5, $6, 0)
		ON CONFLICT (inbox_id) DO UPDATE SET attempted = $3, admitted = $4, confirmed = $5, error = $6`,
		inboxID, uuid.New(), notice.Attempted, notice.Admitted, notice.Confirmed, notice.Error)
}

func (f fixture) entryCreatedAt(t *testing.T, inboxID uuid.UUID, cursor int64) time.Time {
	t.Helper()
	var createdAt time.Time
	if err := f.db.QueryRowContext(t.Context(), `SELECT created_at FROM entry WHERE inbox_id = $1 AND cursor = $2`,
		inboxID, cursor).Scan(&createdAt); err != nil {
		t.Fatal(err)
	}
	return createdAt.UTC()
}

func TestSandboxStatusSummarizesWhatAwaitsAcknowledgementWithoutItsContent(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	subscription := f.subscribeIn(t, "first", uniqueSession(), ref)
	request := subscription.Source.(model.ActionsSource).RequestID
	for sequence := int64(1); sequence <= 3; sequence++ {
		event := storagecodec.ActionEvent{
			Sequence: sequence, State: action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING,
			At: time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC),
			Actor: &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{
				Issuer: "private-issuer", Subject: "private-subject",
			}}},
		}
		payload, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, model.ActionsEvent{RequestID: request, Sequence: sequence}, string(payload))
	}
	summaries := func(cursors ...int64) []EntrySummary {
		var want []EntrySummary
		for _, cursor := range cursors {
			want = append(want, EntrySummary{
				Cursor: cursor, CreatedAt: f.entryCreatedAt(t, subscription.InboxID, cursor),
				Event: model.ActionsEvent{RequestID: request, Sequence: cursor}, ActionState: ptr("decision_pending"),
			})
		}
		return want
	}

	inbox := f.onlyInbox(t, ref)
	requireEqual(t, [2]int64{inbox.UnannouncedCount, inbox.PendingAcknowledgementCount}, [2]int64{3, 3})
	requireEqual(t, inbox.PendingEntries, summaries(1, 2, 3))

	// A notice that names two entries announces them, but the agent has not acknowledged them.
	f.exec(t, `UPDATE inbox SET covered = 2 WHERE id = $1`, subscription.InboxID)
	inbox = f.onlyInbox(t, ref)
	requireEqual(t, [2]int64{inbox.UnannouncedCount, inbox.PendingAcknowledgementCount}, [2]int64{1, 3})
	requireEqual(t, inbox.PendingEntries, summaries(1, 2, 3))

	f.acknowledge(t, subscription.InboxID, 2)
	inbox = f.onlyInbox(t, ref)
	requireEqual(t, [2]int64{inbox.UnannouncedCount, inbox.PendingAcknowledgementCount}, [2]int64{1, 1})
	requireEqual(t, inbox.PendingEntries, summaries(3))

	// Entries whose content expired are neither announced nor awaiting acknowledgement.
	f.exec(t, `UPDATE inbox SET expired_through = 3 WHERE id = $1`, subscription.InboxID)
	inbox = f.onlyInbox(t, ref)
	requireEqual(t, [2]int64{inbox.UnannouncedCount, inbox.PendingAcknowledgementCount}, [2]int64{0, 0})
	requireEqual(t, len(inbox.PendingEntries), 0)
}

func TestSandboxStatusSummarizesTypedActionContent(t *testing.T) {
	f := newIsolatedFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	subscription := f.subscribeIn(t, "typed-summary", uniqueSession(), ref)
	event := model.ActionsEvent{RequestID: subscription.Source.(model.ActionsSource).RequestID, Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	inbox := f.onlyInbox(t, ref)
	requireEqual(t, inbox.PendingEntries, []EntrySummary{{
		Cursor: 1, CreatedAt: f.entryCreatedAt(t, subscription.InboxID, 1), Event: event, ActionState: ptr("decision_pending"),
	}})
}

func TestSandboxStatusSummarizesMessageWithoutLeakingItsBody(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	subscription := f.subscribeIn(t, "message-summary", uniqueSession(), ref)
	event := model.MessageEvent{MessageID: uuid.New(), Sender: model.ServiceAccount{Namespace: "sender", Name: "sandbox"}}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, `{"body":"private message body"}`)
	inbox := f.onlyInbox(t, ref)
	requireEqual(t, inbox.PendingEntries, []EntrySummary{{
		Cursor: 1, CreatedAt: f.entryCreatedAt(t, subscription.InboxID, 1), Event: event,
	}})
}

func TestSandboxStatusDescribesAtMostAHundredPendingEntries(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	subscription := f.subscribeIn(t, "first", uniqueSession(), ref)
	f.addEvents(t, subscription, 1, maxPendingEntrySummaries+1)

	inbox := f.onlyInbox(t, ref)

	requireEqual(t, inbox.PendingAcknowledgementCount, int64(maxPendingEntrySummaries+1))
	requireEqual(t, len(inbox.PendingEntries), maxPendingEntrySummaries)
	requireEqual(t, inbox.PendingEntries[maxPendingEntrySummaries-1].Cursor, int64(maxPendingEntrySummaries))
}

func TestSandboxStatusShowsEachSubscriptionAndWhenItsSourceIsNextRead(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	session := uniqueSession()
	failing := f.subscribeIn(t, "failing", session, ref)
	cancelled := f.subscribeIn(t, "cancelled", session, ref)
	f.cancel(t, cancelled.ID)
	for _, id := range []uuid.UUID{failing.ID, cancelled.ID} {
		f.exec(t, `
			UPDATE subscription SET error = 'Actions Service unavailable', error_kind = 'unavailable',
				error_since = now() - interval '2 minutes', error_observed_at = now(), next_attempt = now() + interval '1 minute'
			WHERE id = $1`, id)
	}

	inbox := f.onlyInbox(t, ref)

	byID := map[uuid.UUID]Subscription{}
	for _, status := range inbox.Subscriptions {
		byID[status.ID] = status
	}
	requireEqual(t, len(byID), 2)
	for _, id := range []uuid.UUID{failing.ID, cancelled.ID} {
		status := byID[id]
		if status.Failure == nil || status.Failure.Kind != FailureUnavailable || status.Failure.Message != "Actions Service unavailable" {
			t.Errorf("subscription %s failure = %+v", id, status.Failure)
		}
		requireBetween(t, "NextSourceCheckAt", status.NextSourceCheckAt, time.Now().Add(55*time.Second), time.Now().Add(time.Minute))
	}
	requireEqual(t, byID[failing.ID].Cancelled, false)
	requireEqual(t, byID[failing.ID].RetryAt, byID[failing.ID].NextSourceCheckAt)
	// A cancelled subscription is not retried, though the row still says when its source would have been read.
	requireEqual(t, byID[cancelled.ID].Cancelled, true)
	requireEqual(t, byID[cancelled.ID].RetryAt, (*time.Time)(nil))
	requireEqual(t, inbox.Subscriptions[0].ID.String() < inbox.Subscriptions[1].ID.String(), true)
}

func TestSandboxStatusSaysWhyTheNextNoticeWaits(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	subscription := f.subscribeIn(t, "first", uniqueSession(), ref)
	wait := func() NoticeWait { return f.onlyInbox(t, ref).NoticeWait }
	reason := func(want NoticeWaitReason) {
		t.Helper()
		got := wait()
		if got.Reason == nil || *got.Reason != want {
			t.Fatalf("reason = %v, want %s", got.Reason, want)
		}
	}
	requireEqual(t, wait(), NoticeWait{})

	f.addEvents(t, subscription, 1, 1)
	created := f.entryCreatedAt(t, subscription.InboxID, 1)
	requireEqual(t, wait(), NoticeWait{Reason: ptr(WaitDebouncing), Window: &Window{
		QuietUntil: created.Add(time.Minute), MaxWaitAt: created.Add(2 * time.Minute), Due: created.Add(time.Minute),
	}})

	f.exec(t, `UPDATE entry SET created_at = now() - interval '10 minutes' WHERE inbox_id = $1`, subscription.InboxID)
	reason(WaitReady)
	requireEqual(t, wait().Window, (*Window)(nil))

	f.setNotice(t, subscription.InboxID, Notice{Attempted: true})
	f.exec(t, `UPDATE inbox SET covered = 1 WHERE id = $1`, subscription.InboxID)
	reason(WaitAwaitingConfirmation)

	f.exec(t, `UPDATE inbox SET delivery_error = 'ServiceError: Unavailable' WHERE id = $1`, subscription.InboxID)
	reason(WaitDeliveryRetry)

	f.setNotice(t, subscription.InboxID, Notice{Attempted: true, Error: ptr("runner command failed")})
	reason(WaitNoticeError)

	if err := f.Retire(t.Context(), f.incarnation(), subscription.InboxID); err != nil {
		t.Fatal(err)
	}
	reason(WaitRetired)
}

func TestSessionStatusRestrictsNamespaceAndCurrentOwner(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	creation := f.body("status-owner")
	sub := f.subscribe(t, creation)
	read := func(namespace, session string, owner Owner) *InboxStatus {
		t.Helper()
		got, _, err := f.SessionStatus(t.Context(), namespace, session, owner, statusDebounce, true, true)
		if err != nil {
			t.Fatal(err)
		}
		return got
	}
	if got := read("test-namespace", f.sessionID, nil); got == nil || got.Inbox.ID != sub.InboxID {
		t.Fatalf("operator got %+v", got)
	}
	owner := f.incarnation()
	if got := read("test-namespace", f.sessionID, owner); got == nil || got.Inbox.ID != sub.InboxID {
		t.Fatalf("owner got %+v", got)
	}
	other := owner
	other.Account = newOwner()
	if got := read("test-namespace", f.sessionID, other); got != nil {
		t.Fatalf("other account got %+v", got)
	}
	other = owner
	other.SandboxUID = "replacement"
	if got := read("test-namespace", f.sessionID, other); got != nil {
		t.Fatalf("replacement got %+v", got)
	}
	if got := read("other-namespace", f.sessionID, nil); got != nil {
		t.Fatalf("other namespace got %+v", got)
	}
}

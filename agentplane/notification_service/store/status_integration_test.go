package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"testing"
	"time"

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

func (f fixture) sandboxStatus(t *testing.T, ref model.DestinationRef) SandboxNotificationStatus {
	t.Helper()
	status, err := f.SandboxStatus(t.Context(), ref, statusDebounce)
	if err != nil {
		t.Fatal(err)
	}
	return status
}

func (f fixture) onlyInbox(t *testing.T, ref model.DestinationRef) InboxStatus {
	t.Helper()
	status := f.sandboxStatus(t, ref)
	if len(status.Inboxes) != 1 {
		t.Fatalf("%d inboxes, want 1", len(status.Inboxes))
	}
	return status.Inboxes[0]
}

func (f fixture) noticeStates(t *testing.T, namespace string, sessionIDs ...string) SessionNoticeStates {
	t.Helper()
	states, err := f.SessionNoticeStates(t.Context(), namespace, sessionIDs, statusDebounce)
	if err != nil {
		t.Fatal(err)
	}
	return states
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

func TestSandboxStatusIsPinnedToTheSandboxIncarnationAndListsEveryOwnersInboxes(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	second := f.subscribeIn(t, "second", "b-session", ref)
	first := f.subscribeIn(t, "first", "a-session", ref)
	other := f
	other.owner = newOwner()
	retired := other.subscribeIn(t, "retired", "c-session", ref)
	if err := other.Retire(t.Context(), other.incarnation(), retired.InboxID); err != nil {
		t.Fatal(err)
	}

	status := f.sandboxStatus(t, ref)

	var got []uuid.UUID
	for _, inbox := range status.Inboxes {
		got = append(got, inbox.Inbox.ID)
	}
	// In Session order, retired inboxes included, whoever owns them.
	requireEqual(t, got, []uuid.UUID{first.InboxID, second.InboxID, retired.InboxID})
	requireEqual(t, status.Inboxes[2].Inbox.Retired, true)
	requireEqual(t, status.Inboxes[0].Inbox.DestinationRef, ref)
	if time.Since(status.ObservedAt) > time.Minute {
		t.Errorf("observed at %s", status.ObservedAt)
	}

	// Another incarnation of the Sandbox, or another Sandbox, has none of them.
	for name, changed := range map[string]model.DestinationRef{
		"another UID":       {Namespace: ref.Namespace, Name: ref.Name, UID: "another-uid"},
		"another name":      {Namespace: ref.Namespace, Name: "another-sandbox", UID: ref.UID},
		"another namespace": {Namespace: "another-namespace", Name: ref.Name, UID: ref.UID},
	} {
		if inboxes := f.sandboxStatus(t, changed).Inboxes; len(inboxes) != 0 {
			t.Errorf("%s: %d inboxes, want none", name, len(inboxes))
		}
	}
}

func TestSandboxStatusSummarizesWhatAwaitsAcknowledgementWithoutItsContent(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	subscription := f.subscribeIn(t, "first", uniqueSession(), ref)
	request := subscription.Source.(model.ActionsSource).RequestID
	for sequence := int64(1); sequence <= 3; sequence++ {
		event := ActionEvent{
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
				Cursor: cursor, CreatedAt: f.entryCreatedAt(t, subscription.InboxID, cursor), Provider: EntryFromActions,
				Summary: fmt.Sprintf("Action %s · event %d · decision_pending", request, cursor),
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

func TestSandboxStatusSummarizesTypedContentWhenLegacyJSONConflicts(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	subscription := f.subscribeIn(t, "typed-summary", uniqueSession(), ref)
	event := model.ActionsEvent{RequestID: subscription.Source.(model.ActionsSource).RequestID, Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	f.exec(t, `UPDATE entry SET payload = '{"state":"legacy_conflict"}' WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID)

	inbox := f.onlyInbox(t, ref)
	requireEqual(t, inbox.PendingEntries, []EntrySummary{{
		Cursor: 1, CreatedAt: f.entryCreatedAt(t, subscription.InboxID, 1), Provider: EntryFromActions,
		Summary: fmt.Sprintf("Action %s · event 1 · decision_pending", event.RequestID),
	}})
}

func TestSandboxStatusSummarizesMessageWithoutLoadingAnyBodyEncoding(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	subscription := f.subscribeIn(t, "message-summary", uniqueSession(), ref)
	event := model.MessageEvent{MessageID: uuid.New(), Sender: model.ServiceAccount{Namespace: "sender", Name: "sandbox"}}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, `{"body":"private message body"}`)
	f.exec(t, `UPDATE entry SET payload = NULL, typed_payload = NULL, message_body = NULL, message_from_session = NULL
		WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID)

	inbox := f.onlyInbox(t, ref)
	requireEqual(t, inbox.PendingEntries, []EntrySummary{{
		Cursor: 1, CreatedAt: f.entryCreatedAt(t, subscription.InboxID, 1), Provider: EntryFromMessage,
		Summary: "Message from sender/sandbox",
	}})
}

func TestSandboxStatusSummarizesNativeGitHubObservationPayload(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	ref := uniqueSandbox()
	f.sandboxUID = ref.UID
	subscription := f.subscribeIn(t, "github-observation-summary", uniqueSession(), ref)
	event := model.GitHubObservationEvent{AppID: 1, RepositoryID: 2, Event: model.EventCheckRun, Sequence: 4}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))

	inbox := f.onlyInbox(t, ref)
	requireEqual(t, inbox.PendingEntries, []EntrySummary{{
		Cursor: 1, CreatedAt: f.entryCreatedAt(t, subscription.InboxID, 1), Provider: EntryFromGitHub,
		Summary: "GitHub check_run observation · sequence 4",
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

	byID := map[uuid.UUID]SubscriptionStatus{}
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

func TestSessionNoticeStatesCountEntriesByWhatHasHappenedToThemWithoutAcknowledging(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	session := uniqueSession()
	subscription := f.subscribeIn(t, "first", session, body("first").DestinationRef)

	initial := f.noticeStates(t, "test-namespace", session, "unknown-"+uuid.NewString())
	requireEqual(t, len(initial.Inboxes), 1)
	requireEqual(t, initial.Inboxes[0], InboxNoticeState{InboxID: subscription.InboxID, SessionID: session})

	f.addEvents(t, subscription, 1, 3)
	created := f.entryCreatedAt(t, subscription.InboxID, 3)
	queued := f.noticeStates(t, "test-namespace", session).Inboxes[0]
	requireEqual(t, [2]int64{queued.Queued, queued.Unacknowledged}, [2]int64{3, 0})
	requireEqual(t, queued.Notice, (*NoticeProgress)(nil))
	// The newest entry sets the quiet window.
	requireEqual(t, queued.NoticeWait.Window.QuietUntil, created.Add(time.Minute))
	requireEqual(t, queued.NoticeWait.Reason, ptr(WaitDebouncing))

	// Reading acknowledges nothing, however often it happens.
	f.noticeStates(t, "test-namespace", session)
	requireEqual(t, f.read(t, subscription.InboxID, 0).Inbox.Acknowledged, int64(0))

	// A notice naming two entries moves them from queued to unacknowledged.
	f.setNotice(t, subscription.InboxID, Notice{Attempted: true})
	f.exec(t, `UPDATE inbox SET covered = 2 WHERE id = $1`, subscription.InboxID)
	named := f.noticeStates(t, "test-namespace", session).Inboxes[0]
	requireEqual(t, [2]int64{named.Queued, named.Unacknowledged}, [2]int64{1, 2})
	requireEqual(t, named.Notice, ptr(NoticeAttempted))
	requireEqual(t, named.NoticeWait.Reason, ptr(WaitAwaitingConfirmation))
	requireEqual(t, named.NoticeWait.Window, (*Window)(nil))

	f.setNotice(t, subscription.InboxID, Notice{Attempted: true, Admitted: true, Confirmed: true})
	f.acknowledge(t, subscription.InboxID, 3)
	done := f.noticeStates(t, "test-namespace", session).Inboxes[0]
	requireEqual(t, [2]int64{done.Queued, done.Unacknowledged}, [2]int64{0, 0})
	requireEqual(t, done.NoticeWait, NoticeWait{})
}

func TestSessionNoticeStatesShowHowFarTheLatestNoticeGot(t *testing.T) {
	for _, c := range []struct {
		name   string
		notice Notice
		want   NoticeProgress
	}{
		{"prepared", Notice{}, NoticePrepared},
		{"attempted", Notice{Attempted: true}, NoticeAttempted},
		{"admitted", Notice{Attempted: true, Admitted: true}, NoticeAdmitted},
		{"confirmed", Notice{Attempted: true, Admitted: true, Confirmed: true}, NoticeConfirmed},
		// Confirmation wins however the flags came about.
		{"confirmed without an admission", Notice{Confirmed: true}, NoticeConfirmed},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newFixture(t, defaultQuotas())
			session := uniqueSession()
			subscription := f.subscribeIn(t, "first", session, body("first").DestinationRef)
			f.setNotice(t, subscription.InboxID, c.notice)

			state := f.noticeStates(t, "test-namespace", session).Inboxes[0]

			requireEqual(t, state.Notice, &c.want)
		})
	}
}

func TestSessionNoticeStatesShowTheNoticeErrorBeforeTheDeliveryError(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	session := uniqueSession()
	subscription := f.subscribeIn(t, "first", session, body("first").DestinationRef)
	errorOf := func() *string { return f.noticeStates(t, "test-namespace", session).Inboxes[0].Error }
	requireEqual(t, errorOf(), (*string)(nil))

	f.exec(t, `UPDATE inbox SET delivery_error = 'ServiceError: Unavailable' WHERE id = $1`, subscription.InboxID)
	requireEqual(t, errorOf(), ptr("ServiceError: Unavailable"))

	f.setNotice(t, subscription.InboxID, Notice{Error: ptr("runner command failed")})
	requireEqual(t, errorOf(), ptr("runner command failed"))
}

func TestSessionNoticeStatesAreLiveInboxesOfTheNamedSessionsInTheNamespaceWhoeverOwnsThem(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	other := f
	other.owner = newOwner()
	b, c, d := uniqueSession(), uniqueSession(), uniqueSession()
	// Session order is the order of the IDs.
	sessions := []string{b, c, d}
	slices.Sort(sessions)
	owned := f.subscribeIn(t, "owned", sessions[0], body("owned").DestinationRef)
	others := other.subscribeIn(t, "others", sessions[1], body("others").DestinationRef)
	retired := other.subscribeIn(t, "retired", sessions[2], body("retired").DestinationRef)
	if err := other.Retire(t.Context(), other.incarnation(), retired.InboxID); err != nil {
		t.Fatal(err)
	}
	elsewhere := body("elsewhere").DestinationRef
	elsewhere.Namespace = "another-namespace"
	f.subscribeIn(t, "elsewhere", sessions[0], elsewhere)

	states := f.noticeStates(t, "test-namespace", sessions...)

	var got []uuid.UUID
	for _, state := range states.Inboxes {
		got = append(got, state.InboxID)
	}
	requireEqual(t, got, []uuid.UUID{owned.InboxID, others.InboxID})
	requireEqual(t, len(f.noticeStates(t, "another-namespace", sessions...).Inboxes), 1)
	requireEqual(t, len(f.noticeStates(t, "test-namespace").Inboxes), 0)
}

func TestSessionNoticeStatesReadAtMostAFixedNumberOfDistinctSessions(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	sessions := make([]string, MaxNoticeStateSessions)
	for i := range sessions {
		sessions[i] = fmt.Sprintf("session-%d", i)
	}
	if _, err := f.SessionNoticeStates(t.Context(), "test-namespace", sessions, statusDebounce); err != nil {
		t.Fatalf("the most Sessions failed: %v", err)
	}
	// Repeats of one Session are one Session.
	repeats := append([]string{"session-0"}, sessions...)
	if _, err := f.SessionNoticeStates(t.Context(), "test-namespace", repeats, statusDebounce); err != nil {
		t.Fatalf("repeated Sessions failed: %v", err)
	}

	_, err := f.SessionNoticeStates(t.Context(), "test-namespace", append(sessions, "one-too-many"), statusDebounce)

	if !errors.Is(err, model.ErrInvalid) {
		t.Fatalf("error = %v, want model.ErrInvalid", err)
	}
}

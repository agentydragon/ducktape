package worker

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

func debounce(quiet, maxWait float64) Settings {
	return Settings{NoticeDebounce: NoticeDebounce{QuietSeconds: quiet, MaxWaitSeconds: maxWait}, StaleInboxConfirmationSeconds: 30}
}

func TestNoticeWaitsOutTheQuietWindowAcrossRestartsWhileReadsAreImmediate(t *testing.T) {
	h := newHarness(t, debounce(60, 300))
	request := uuid.New()
	subscription := h.subscribeTo(t, "first", request)
	overlap := h.subscribeTo(t, "overlap", request)
	h.addEvents(t, subscription, 1, 2)
	now := time.Now().Truncate(time.Microsecond)
	last := now.Add(-time.Second)
	h.setEntryTime(t, subscription.InboxID, 1, now.Add(-120*time.Second))
	h.setEntryTime(t, subscription.InboxID, 2, last)
	// Matching the same events through another subscription must not extend the quiet window.
	h.addEvents(t, overlap, 1, 2)
	requireEqual(t, h.page(t, subscription.InboxID).Inbox.LastCursor, int64(2))
	// Without source deadlines, only the notice schedules the inbox.
	for _, id := range []uuid.UUID{subscription.ID, overlap.ID} {
		if _, err := h.store.Cancel(t.Context(), testIncarnation, id); err != nil {
			t.Fatal(err)
		}
	}

	h.step(t)
	page := h.page(t, subscription.InboxID)
	requireEqual(t, len(page.Entries), 2)
	requireEqual(t, page.Inbox.Acknowledged, int64(0))
	if page.Notice != nil {
		t.Fatalf("a notice was prepared inside the quiet window: %+v", page.Notice)
	}
	due := last.Add(60 * time.Second)
	requireWithin(t, "next attempt", h.row(t, subscription.InboxID).nextAttempt, due, due)

	// A new process finds the deadline in the rows: it needs no timer of the old one and no new wakeup.
	h.restart()
	next, err := h.store.NextWorkAt(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	requireWithin(t, "next work", next, due, due)
	h.requireNoneDue(t)

	// A claim before the window has run out finds nothing to announce and keeps the deadline.
	h.makeDue(t, subscription.InboxID)
	h.step(t)
	if h.page(t, subscription.InboxID).Notice != nil {
		t.Fatal("a notice was prepared inside the quiet window after a restart")
	}
	requireWithin(t, "next attempt", h.row(t, subscription.InboxID).nextAttempt, due, due)

	h.setEntryTime(t, subscription.InboxID, 2, time.Now().Add(-61*time.Second))
	h.makeDue(t, subscription.InboxID)
	h.step(t)
	requireEqual(t, h.notice(t, subscription.InboxID).ThroughCursor, int64(2))
}

func TestWithoutDebounceTheNoticeIsPreparedAndSubmittedAtOnce(t *testing.T) {
	h := newHarness(t, debounce(0, 300))
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 2)

	h.step(t)

	notice := h.notice(t, subscription.InboxID)
	requireEqual(t, notice.ThroughCursor, int64(2))
	requireEqual(t, notice.Attempted, true)
	requests := h.dispatcher.submitted()
	requireEqual(t, len(requests), 1)
	requireEqual(t, [4]string{requests[0].GetSandbox(), requests[0].GetSessionId(), requests[0].GetCommandId(), requests[0].GetText()},
		[4]string{testSandbox, testSession, notice.CommandID.String(), notice.Text})
}

func TestMaxWaitCapsTheQuietWindowAndANoticeInFlightIsNotExtended(t *testing.T) {
	h := newHarness(t, debounce(60, 300))
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 2)
	inbox := subscription.InboxID

	h.makeDue(t, inbox)
	h.step(t)
	if h.page(t, inbox).Notice != nil {
		t.Fatal("a notice was prepared inside the quiet window")
	}

	// A recent entry cannot postpone an older pending one beyond the maximum wait.
	h.setEntryTime(t, inbox, 1, time.Now().Add(-301*time.Second))
	h.makeDue(t, inbox)
	h.step(t)
	notice := h.notice(t, inbox)
	requireEqual(t, notice.ThroughCursor, int64(2))

	// New entries do not change the notice in flight: its retry is the same command.
	h.addEvents(t, subscription, 3, 3)
	h.makeDue(t, inbox)
	h.step(t)
	requireEqual(t, h.notice(t, inbox), notice)
	requests := h.dispatcher.submitted()
	requireEqual(t, len(requests), 2)
	requireEqual(t, requests[1].GetCommandId(), requests[0].GetCommandId())
	requireEqual(t, requests[1].GetText(), requests[0].GetText())

	h.history.add(confirmedEntry(1, notice.CommandID.String()))
	h.makeDue(t, inbox)
	h.step(t)
	requireEqual(t, h.notice(t, inbox).Confirmed, true)

	// The next burst has a window of its own; the old covered entry cannot force it due.
	h.makeDue(t, inbox)
	h.step(t)
	requireEqual(t, h.notice(t, inbox).CommandID, notice.CommandID)
	requireEqual(t, len(h.dispatcher.submitted()), 2)

	if _, err := h.store.Cancel(t.Context(), testIncarnation, subscription.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := h.store.Acknowledge(t.Context(), testIncarnation, inbox, 3); err != nil {
		t.Fatal(err)
	}
	h.makeDue(t, inbox)
	h.step(t)
	next, err := h.store.NextWorkAt(t.Context())
	if err != nil || next != nil {
		t.Fatalf("NextWorkAt = %v, %v; want no work", next, err)
	}
}

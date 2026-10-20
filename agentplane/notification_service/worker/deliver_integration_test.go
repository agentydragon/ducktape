package worker

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func (h *harness) deliver(t *testing.T, claim store.Claim, notice store.Notice) error {
	t.Helper()
	return h.worker.deliver(t.Context(), claim, testSandbox, notice)
}

func (h *harness) mustDeliver(t *testing.T, claim store.Claim, notice store.Notice) {
	t.Helper()
	if err := h.deliver(t, claim, notice); err != nil {
		t.Fatal(err)
	}
}

// attempted marks the prepared notice as attempted, as a first delivery does, and returns it as stored.
func (h *harness) attempted(t *testing.T, claim store.Claim, notice store.Notice) store.Notice {
	t.Helper()
	if attempt, err := h.store.Attempt(t.Context(), claim, notice); err != nil || !attempt {
		t.Fatalf("Attempt = %v, %v", attempt, err)
	}
	current, err := h.store.CurrentNotice(t.Context(), claim)
	if err != nil || current == nil {
		t.Fatalf("CurrentNotice = %+v, %v", current, err)
	}
	return *current
}

func submissionOf(notice store.Notice) *dispatcherpb.SubmitInputRequest {
	return &dispatcherpb.SubmitInputRequest{
		Sandbox: testSandbox, SessionId: testSession, CommandId: notice.CommandID.String(), Text: notice.Text,
	}
}

func requireSubmitted(t *testing.T, h *harness, want ...*dispatcherpb.SubmitInputRequest) {
	t.Helper()
	got := h.dispatcher.submitted()
	if len(got) != len(want) {
		t.Fatalf("%d submissions, want %d: %v", len(got), len(want), got)
	}
	for i := range want {
		if !proto.Equal(got[i], want[i]) {
			t.Errorf("submission %d = %v, want %v", i, got[i], want[i])
		}
	}
}

func requireInvalidHistory(t *testing.T, err error) {
	t.Helper()
	var invalid *store.InvalidHistoryError
	if !errors.As(err, &invalid) {
		t.Fatalf("error = %v, want *store.InvalidHistoryError", err)
	}
}

func TestAcknowledgementBeforeDispatchSuppressesTheNotice(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	claim, subscription, notice := h.prepared(t, 1)
	if _, err := h.store.Acknowledge(t.Context(), testOwner, subscription.InboxID, notice.ThroughCursor); err != nil {
		t.Fatal(err)
	}

	h.mustDeliver(t, claim, notice)

	suppressed := h.notice(t, subscription.InboxID)
	if suppressed.Attempted || suppressed.Error == nil {
		t.Errorf("notice = %+v, want an ended notice that was never attempted", suppressed)
	}
	requireSubmitted(t, h)
}

func TestDispatcherRetriesKeepTheCommandAndQueuedIsNotAnAdmission(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	claim, subscription, notice := h.prepared(t, 1)

	h.mustDeliver(t, claim, notice)
	h.mustDeliver(t, claim, notice)

	requireSubmitted(t, h, submissionOf(notice), submissionOf(notice))
	current := h.notice(t, subscription.InboxID)
	requireEqual(t, [4]bool{current.Attempted, current.Admitted, current.Confirmed, current.Error != nil}, [4]bool{true, false, false, false})
}

func TestDispatcherOutcomesAreEitherTerminalOrWaitedFor(t *testing.T) {
	for _, c := range []struct {
		name   string
		state  dispatcherpb.DeliveryState
		reason *string
		// want is the error the notice ends with, or nil while the notice waits for the runner's receipts.
		want *string
	}{
		{"failed", dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED, ptr("sandbox_deleted"), ptr("Dispatcher DELIVERY_STATE_FAILED: sandbox_deleted")},
		{"no-op", dispatcherpb.DeliveryState_DELIVERY_STATE_NO_OP, ptr("nothing to do"), ptr("Dispatcher DELIVERY_STATE_NO_OP: nothing to do")},
		{"cancelled", dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER, ptr("operator"), ptr("Dispatcher DELIVERY_STATE_CANCELLED_IN_DISPATCHER: operator")},
		{"unknown without a reason", dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN, nil, ptr("Dispatcher DELIVERY_STATE_UNKNOWN: no reason provided")},
		{"queued", dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER, ptr("waiting for a runner"), nil},
		{"delivering", dispatcherpb.DeliveryState_DELIVERY_STATE_DELIVERING_TO_RUNNER, nil, nil},
		{"admitted", dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED, nil, nil},
		{"confirmed", dispatcherpb.DeliveryState_DELIVERY_STATE_HARNESS_CONFIRMED, nil, nil},
	} {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t, debounce(3600, 3600))
			claim, subscription, notice := h.prepared(t, 1)
			h.dispatcher.respond = func(context.Context, *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
				return dispatched(c.state, c.reason), nil
			}

			h.mustDeliver(t, claim, notice)

			current := h.notice(t, subscription.InboxID)
			requireEqual(t, current.Error, c.want)
			// Only History's receipts say what the runner did with it.
			requireEqual(t, [3]bool{current.Attempted, current.Admitted, current.Confirmed}, [3]bool{true, false, false})
		})
	}
}

func TestADispatcherFailureIsRetriedAndShownAsAServiceError(t *testing.T) {
	h := newHarness(t, debounce(0, 300))
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 1)
	h.dispatcher.respond = func(context.Context, *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
		return nil, status.Error(codes.Unauthenticated, "unauthenticated")
	}

	h.step(t)

	row := h.row(t, subscription.InboxID)
	requireEqual(t, row.deliveryError, ptr("ServiceError: Unauthenticated"))
	notice := h.notice(t, subscription.InboxID)
	requireEqual(t, [2]bool{notice.Attempted, notice.Error != nil}, [2]bool{true, false})
	h.requireNoneDue(t)

	// The next claim submits the same command again.
	h.dispatcher.respond = nil
	h.makeDue(t, subscription.InboxID)
	h.step(t)
	requireSubmitted(t, h, submissionOf(notice), submissionOf(notice))
	requireEqual(t, h.row(t, subscription.InboxID).deliveryError, (*string)(nil))
}

func TestDeliveryNeverSendsTheNoticeToAHistoryThatCannotBeFollowed(t *testing.T) {
	h := newHarness(t, debounce(0, 300))
	claim, subscription, notice := h.prepared(t, 1)
	// The notice's copied cursor is ahead of the History's watermark.
	if err := h.store.CheckpointBeforeAttempt(t.Context(), claim, notice, unrelatedEntry(5)); err != nil {
		t.Fatal(err)
	}
	if err := h.store.Release(t.Context(), claim, nil, nil); err != nil {
		t.Fatal(err)
	}
	h.history.add(unrelatedEntry(3))
	h.makeDue(t, subscription.InboxID)

	h.step(t)

	invalid := h.row(t, subscription.InboxID).deliveryError
	if invalid == nil || !strings.HasPrefix(*invalid, store.InvalidHistoryPrefix) {
		t.Fatalf("delivery error = %v, want one that starts with %q", invalid, store.InvalidHistoryPrefix)
	}
	requireSubmitted(t, h)

	// The inbox stays so, even when History looks sound again.
	h.history.add(unrelatedEntry(10))
	h.makeDue(t, subscription.InboxID)
	h.step(t)
	requireEqual(t, h.row(t, subscription.InboxID).deliveryError, invalid)
	requireSubmitted(t, h)
}

func TestHistoryBeforeTheFirstAttemptIsSkippedWithoutReadingIt(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	for cursor := uint64(1); cursor <= 10; cursor++ {
		h.history.add(unrelatedEntry(cursor))
	}
	claim, subscription, notice := h.prepared(t, 1)

	h.mustDeliver(t, claim, notice)

	current := h.notice(t, subscription.InboxID)
	requireEqual(t, [2]any{current.Attempted, current.RunnerCursor}, [2]any{true, int64(10)})
	requireSubmitted(t, h, submissionOf(notice))
	for _, request := range h.history.readRequests() {
		if request.GetLimit() != 1 && request.GetAfterCursor() < 10 {
			t.Errorf("read %d entries after %d, from before the tail", request.GetLimit(), request.GetAfterCursor())
		}
	}
}

func TestHistoryAfterAnAttemptIsNeverSkipped(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	claim, subscription, notice := h.prepared(t, 1)
	notice = h.attempted(t, claim, notice)
	command := notice.CommandID.String()
	h.history.add(unrelatedEntry(1), unrelatedEntry(2), unrelatedEntry(3), admittedEntry(4, command, notice.Text), unrelatedEntry(5), unrelatedEntry(6))

	h.mustDeliver(t, claim, notice)

	// The admission was found in history, so the notice is not submitted again.
	current := h.notice(t, subscription.InboxID)
	requireEqual(t, [3]any{current.RunnerCursor, current.Admitted, current.Confirmed}, [3]any{int64(6), true, false})
	requireSubmitted(t, h)

	h.history.add(confirmedEntry(7, command))
	h.mustDeliver(t, claim, current)
	requireEqual(t, h.notice(t, subscription.InboxID).Confirmed, true)
	requireSubmitted(t, h)
}

func TestCatchUpIsBoundedPerClaimAndTheNoticeWaitsForTheWholeHistory(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	claim, subscription, notice := h.prepared(t, 1)
	notice = h.attempted(t, claim, notice)
	for cursor := uint64(1); cursor <= 130; cursor++ {
		h.history.add(unrelatedEntry(cursor))
	}

	h.mustDeliver(t, claim, notice)
	partial := h.notice(t, subscription.InboxID)
	requireEqual(t, partial.RunnerCursor, int64(historyPage))
	// A resubmission could precede an admission History has not shown yet.
	requireSubmitted(t, h)

	h.mustDeliver(t, claim, partial)
	requireEqual(t, h.notice(t, subscription.InboxID).RunnerCursor, int64(130))
	requireSubmitted(t, h, submissionOf(notice))
}

func TestOnlyCursorsHistoryDroppedMayBeMissingFromTheRunnerEntries(t *testing.T) {
	type span struct{ first, last uint64 }
	for _, c := range []struct {
		name    string
		cursors []uint64
		settled []span
		removed []span
		invalid bool
	}{
		{name: "a gap nobody accounts for", cursors: []uint64{1, 3}, invalid: true},
		{name: "a gap of settled deltas", cursors: []uint64{1, 3}, settled: []span{{2, 2}}},
		{name: "a gap of removed cursors", cursors: []uint64{1, 3}, removed: []span{{2, 2}}},
		{name: "a gap only partly accounted for", cursors: []uint64{1, 4}, settled: []span{{2, 2}}, invalid: true},
		{name: "a gap accounted for by two ranges", cursors: []uint64{1, 4}, settled: []span{{2, 2}}, removed: []span{{3, 3}}},
		{name: "a gap before the first entry", cursors: []uint64{3}, invalid: true},
		{name: "a gap before the first entry that is settled", cursors: []uint64{3}, settled: []span{{1, 2}}},
	} {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t, debounce(3600, 3600))
			claim, subscription, notice := h.prepared(t, 1)
			notice = h.attempted(t, claim, notice)
			for _, cursor := range c.cursors {
				h.history.add(unrelatedEntry(cursor))
			}
			for _, s := range c.settled {
				h.history.drop(s.first, s.last, false)
			}
			for _, s := range c.removed {
				h.history.drop(s.first, s.last, true)
			}

			err := h.deliver(t, claim, notice)

			current := h.notice(t, subscription.InboxID)
			if c.invalid {
				requireInvalidHistory(t, err)
				requireEqual(t, current.RunnerCursor, int64(0))
				requireSubmitted(t, h)
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			requireEqual(t, current.RunnerCursor, int64(c.cursors[len(c.cursors)-1]))
			requireSubmitted(t, h, submissionOf(notice))
		})
	}
}

func TestHistoryThatChangedAtTheCommittedBoundaryIsRefused(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	claim, subscription, notice := h.prepared(t, 1)
	notice = h.attempted(t, claim, notice)
	if err := h.store.Receipt(t.Context(), claim, notice, unrelatedEntry(5), 0); err != nil {
		t.Fatal(err)
	}
	h.history.add(unrelatedEntry(5))
	committed := h.notice(t, subscription.InboxID)

	// After a restart the boundary is verified against History, and found as it was.
	h.mustDeliver(t, claim, committed)
	requireSubmitted(t, h, submissionOf(notice))

	h.history.add(admittedEntry(5, "another-command", "text"))
	requireInvalidHistory(t, h.deliver(t, claim, committed))
	requireSubmitted(t, h, submissionOf(notice))
}

func TestANoticeIsFollowedToConfirmationAndNewEntriesGetAnother(t *testing.T) {
	h := newHarness(t, debounce(0, 300))
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 2)
	inbox := subscription.InboxID

	h.step(t)
	first := h.notice(t, inbox)
	requireSubmitted(t, h, submissionOf(first))

	// The runner admitted it and the harness confirmed it.
	h.history.add(admittedEntry(1, first.CommandID.String(), first.Text), confirmedEntry(2, first.CommandID.String()))
	h.makeDue(t, inbox)
	h.step(t)
	confirmed := h.notice(t, inbox)
	requireEqual(t, [3]any{confirmed.Admitted, confirmed.Confirmed, confirmed.RunnerCursor}, [3]any{true, true, int64(2)})
	requireSubmitted(t, h, submissionOf(first))

	// Entries that came since are announced by a notice of their own, which starts after the history it skips.
	h.addEvents(t, subscription, 3, 3)
	h.makeDue(t, inbox)
	h.step(t)
	second := h.notice(t, inbox)
	if second.CommandID == first.CommandID {
		t.Fatal("the new entry was announced by the confirmed notice")
	}
	requireEqual(t, [3]any{second.ThroughCursor, second.Attempted, second.RunnerCursor}, [3]any{int64(3), true, int64(2)})
	requireSubmitted(t, h, submissionOf(first), submissionOf(second))
}

package worker

import (
	"context"
	"fmt"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc"
)

// History reads a Session's runner entries from the History Service.
type History interface {
	ReadSessionEvents(ctx context.Context, in *historypb.ReadSessionEventsRequest, opts ...grpc.CallOption) (*historypb.ReadSessionEventsResponse, error)
}

// Dispatcher stores a command for the Session Dispatcher to deliver to a Sandbox's runner.
type Dispatcher interface {
	SubmitInput(ctx context.Context, in *dispatcherpb.SubmitInputRequest, opts ...grpc.CallOption) (*dispatcherpb.SubmitInputResponse, error)
}

// historyPage is how many entries one read of History asks for while catching up.
const historyPage = 128

// readHistory returns the Session's entries after afterCursor as History has committed them, at most limit. A
// cursor missing between them is only acceptable if History says it was settled or removed: those drop deltas
// and stop records, and any other missing cursor could have been a receipt.
func (w *Worker) readHistory(ctx context.Context, sessionID string, afterCursor uint64, limit uint32) ([]*eventlogpb.EventEntry, error) {
	page, err := w.History.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{
		SessionId: sessionID, AfterCursor: afterCursor, Limit: limit,
	})
	if err != nil {
		return nil, unavailable(err)
	}
	var settled []*historypb.CursorRange
	for _, settlement := range page.GetSettlements() {
		settled = append(settled, settlement.GetRanges()...)
	}
	settled = append(settled, page.GetRemoved()...)
	previous := afterCursor
	for _, entry := range page.GetEntries() {
		// Walk the missing cursors range by range, so a long settled run costs no more than a short one.
		for missing := previous + 1; missing < entry.GetCursor(); {
			covered := false
			for _, span := range settled {
				if span.GetFirst() <= missing && missing <= span.GetLast() {
					covered, missing = true, span.GetLast()+1
					break
				}
			}
			if !covered {
				return nil, &store.InvalidHistoryError{Reason: "runner history has a gap"}
			}
		}
		previous = entry.GetCursor()
	}
	return page.GetEntries(), nil
}

// deliver advances the notice towards the runner: it copies the receipts History holds, and, once History is
// current, submits the notice if no receipt shows the runner has it.
func (w *Worker) deliver(ctx context.Context, claim store.Claim, sandbox string, notice store.Notice) error {
	// TODO: Observe command-scoped admission outcomes by command ID in the History Service, rather
	// than checkpointing the shared conversation journal.
	// The runner's journal or History Service watermark may be ahead of the copied cursor.
	// The Dispatcher can accept work while a Sandbox is suspended or its harness is stopped.
	// History is the durable cursor watermark; the Dispatcher owns the delivery wait.
	head, err := w.History.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{SessionId: claim.SessionID, Limit: 1})
	if err != nil {
		return unavailable(err)
	}
	lastCursor := head.GetLastCursor()
	if lastCursor < uint64(notice.RunnerCursor) {
		return &store.InvalidHistoryError{Reason: "runner cursor regressed"}
	}
	copied := uint64(notice.RunnerCursor)
	// Verify the durable boundary before advancing, including after a worker restart.
	if copied != 0 {
		boundary, err := w.readHistory(ctx, claim.SessionID, copied-1, 1)
		if err != nil || len(boundary) == 0 {
			return err
		}
		if err := w.Store.Receipt(ctx, claim, notice, boundary[0], int64(copied-1)); err != nil {
			return err
		}
		copied = boundary[0].GetCursor()
	}
	if !notice.Attempted && lastCursor > copied {
		// No command can precede the durable attempt marker. Skip unrelated history,
		// but retain the exact tail entry to verify continuity on an uncertain retry.
		tail, err := w.readHistory(ctx, claim.SessionID, lastCursor-1, 1)
		if err != nil || len(tail) == 0 {
			return err
		}
		if err := w.Store.CheckpointBeforeAttempt(ctx, claim, notice, tail[0]); err != nil {
			return err
		}
		copied = tail[0].GetCursor()
	}
	// Catch up only since the checkpoint, with bounded work per claim. Never skip
	// history after an attempt: it may contain a lost admission or confirmation.
	entries, err := w.readHistory(ctx, claim.SessionID, copied, historyPage)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if err := w.Store.Receipt(ctx, claim, notice, entry, int64(copied)); err != nil {
			return err
		}
		copied = entry.GetCursor()
	}
	// Until History covers the current durable watermark, a retry could precede an admission.
	if copied < lastCursor {
		return nil
	}
	// Re-read durable receipt state. A lost response is uncertain: reconcile History first,
	// then retry only the same command ID and payload. Both enqueue and runner admission are
	// idempotent; a queued response is not an admission receipt.
	current, err := w.Store.CurrentNotice(ctx, claim)
	if err != nil || current == nil || current.CommandID != notice.CommandID || current.Confirmed || current.Error != nil {
		return err
	}
	if current.Admitted {
		return nil
	}
	if !current.Attempted {
		if attempt, err := w.Store.Attempt(ctx, claim, *current); err != nil || !attempt {
			return err
		}
	}
	// Its admission and confirmation are copied by a later claim, once the History Service has them.
	response, err := w.Dispatcher.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
		Sandbox: sandbox, SessionId: claim.SessionID, CommandId: current.CommandID.String(), Text: current.Text,
	})
	if err != nil {
		return unavailable(err)
	}
	dispatch := response.GetDispatch()
	switch dispatch.GetState() {
	case dispatcherpb.DeliveryState_DELIVERY_STATE_NO_OP, dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED,
		dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER, dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN:
		reason := "no reason provided"
		if dispatch.Reason != nil {
			reason = dispatch.GetReason()
		}
		return w.Store.DeliveryFailed(ctx, claim, *current, fmt.Sprintf("Dispatcher %s: %s", dispatch.GetState(), reason))
	}
	return nil
}

// noticeDueAt is when the entries no notice covers become due for one: the quiet window after the newest of
// them, but never later than the maximum wait after the oldest. It reads the durable entry times, so a
// restarted worker agrees, and overlapping matches of one entry do not extend it. Nil when none are pending.
func (w *Worker) noticeDueAt(ctx context.Context, claim store.Claim) (*time.Time, error) {
	pending, err := w.Store.PendingEntryTimes(ctx, claim)
	if err != nil || pending == nil {
		return nil, err
	}
	due := w.Settings.NoticeDebounce.Debounce().Window(*pending).Due
	return &due, nil
}

// prepareNotice returns the notice to deliver, preparing one only if the debounce has run out. A notice in
// flight is delivered regardless: only new notices are debounced, never the retry or confirmation of a
// command.
func (w *Worker) prepareNotice(ctx context.Context, claim store.Claim) (*store.Notice, error) {
	due, err := w.noticeDueAt(ctx, claim)
	if err != nil {
		return nil, err
	}
	var notice *store.Notice
	if due != nil && !due.After(time.Now()) {
		notice, err = w.Store.PrepareNotice(ctx, claim)
	} else {
		notice, err = w.Store.CurrentNotice(ctx, claim)
	}
	if err != nil || notice == nil || notice.Confirmed || notice.Error != nil {
		return nil, err
	}
	return notice, nil
}

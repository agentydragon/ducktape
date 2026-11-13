package worker

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	model "github.com/agentydragon/ducktape/agentplane/notification_service/model"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc/status"
)

func TestProviderFailuresAreShownOnTheSubscriptionAndRetried(t *testing.T) {
	for _, c := range []struct {
		name    string
		failure error
		kind    model.SourceFailureKind
		message string
		retry   time.Duration
	}{
		{
			"default retry", &sources.ProviderError{Kind: model.FailureUnavailable, Message: "Actions Service unavailable"},
			model.FailureUnavailable, "Actions Service unavailable", sources.DefaultRetry,
		},
		{
			"retry the Provider asks for", &sources.ProviderError{Kind: model.FailureRateLimited, Message: "rate limited", RetryAfter: 10 * time.Second},
			model.FailureRateLimited, "rate limited", 10 * time.Second,
		},
		{
			"quota", &store.QuotaError{Quota: store.EntriesPerInbox, Limit: 10},
			model.FailureProcessingError, "QuotaError", sources.DefaultRetry,
		},
	} {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t, debounce(3600, 3600))
			subscription := h.subscribe(t, "first")
			h.actions.returns(c.failure)

			before := time.Now()
			worked, err := h.sourceWorker.ProcessOne(t.Context())
			if err != nil || !worked {
				t.Fatalf("source step = %v, %v", worked, err)
			}
			after := time.Now()

			shown := h.subscription(t, subscription.ID)
			if shown.Failure == nil {
				t.Fatal("the subscription shows no failure")
			}
			requireEqual(t, [2]any{shown.Failure.Kind, shown.Failure.Message}, [2]any{c.kind, c.message})
			requireWithin(t, "retry", shown.RetryAt, before.Add(c.retry), after.Add(c.retry))
			// The destination is fine, so the inbox shows nothing wrong, and the source waits for its retry.
			requireEqual(t, h.row(t, subscription.InboxID).deliveryError, (*string)(nil))
			h.requireNoSourceDue(t)
		})
	}
}

func TestADeferredSourceShowsNoFailureAndWaitsForItsTime(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	until := time.Now().Add(2 * time.Hour).Truncate(time.Microsecond)
	h.actions.returns(&sources.ProviderDeferred{Until: until})

	worked, err := h.sourceWorker.ProcessOne(t.Context())
	if err != nil || !worked {
		t.Fatalf("source step = %v, %v", worked, err)
	}

	shown := h.subscription(t, subscription.ID)
	requireEqual(t, [2]any{shown.Failure, shown.RetryAt}, [2]any{(*model.SourceFailure)(nil), (*time.Time)(nil)})
	next, err := h.store.NextSourceWorkAt(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	requireWithin(t, "next work", next, until, until)
	h.requireNoSourceDue(t)
}

func TestAProviderBreakingItsContractIsReturnedAfterTheInboxIsReleased(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	bug := errors.New("provider bug")
	h.actions.returns(bug)

	worked, err := h.sourceWorker.ProcessOne(t.Context())

	var unclassified *sources.UnclassifiedProviderError
	if !worked || !errors.As(err, &unclassified) || unclassified.SubscriptionID != subscription.ID || !errors.Is(err, bug) {
		t.Fatalf("step = %v, %v; want the Provider's error for subscription %s", worked, err, subscription.ID)
	}
	// It is not shown as a source failure, and the inbox is not held until its lease runs out.
	requireEqual(t, h.subscription(t, subscription.ID).Failure, (*model.SourceFailure)(nil))
	requireEqual(t, h.sourceClaim(t).ID, subscription.InboxID)
}

func TestAProviderWhoseClaimWasLostLeavesTheInboxToItsNewOwner(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	h.actions.on(func(_ context.Context, claim store.Claim, _ store.DueSubscription) error {
		h.exec(t, `UPDATE inbox SET claim = $2, claim_until = now() + interval '1 hour' WHERE id = $1`, claim.ID, uuid.New())
		return store.ErrClaimLost
	})

	worked, err := h.sourceWorker.ProcessOne(t.Context())
	if err != nil || !worked {
		t.Fatalf("source step = %v, %v", worked, err)
	}

	requireEqual(t, h.subscription(t, subscription.ID).Failure, (*model.SourceFailure)(nil))
	// Nothing was released on the new owner's behalf.
	h.requireNoSourceDue(t)
}

func TestShuttingDownGivesTheInboxBackWithoutShowingTheInterruption(t *testing.T) {
	for _, c := range []struct {
		name string
		// notice is whether an entry awaits a notice, with the source read postponed.
		notice bool
		// block makes the call the worker is in block on its context, as a call to another service does.
		block func(h *harness, started chan<- struct{})
	}{
		{"reading a source", false, func(h *harness, started chan<- struct{}) {
			h.actions.on(func(ctx context.Context, _ store.Claim, _ store.DueSubscription) error {
				close(started)
				<-ctx.Done()
				return ctx.Err()
			})
		}},
		{"submitting the notice", true, func(h *harness, started chan<- struct{}) {
			h.dispatcher.respond = func(ctx context.Context, _ *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
				close(started)
				<-ctx.Done()
				return nil, status.FromContextError(ctx.Err()).Err()
			}
		}},
	} {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t, debounce(0, 300))
			subscription := h.subscribe(t, "first")
			if c.notice {
				h.postponeSources(t)
				h.addEvents(t, subscription, 1, 1)
			}
			started := make(chan struct{})
			c.block(h, started)
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			done := make(chan error, 1)
			go func() {
				var err error
				if c.name == "reading a source" {
					_, err = h.sourceWorker.ProcessOne(ctx)
				} else {
					_, err = h.worker.ProcessOne(ctx)
				}
				done <- err
			}()
			<-started
			cancel()

			if err := <-done; !errors.Is(err, context.Canceled) {
				t.Fatalf("step = %v, want context.Canceled", err)
			}
			// Both workers release an interrupted claim promptly instead of waiting for its lease to expire.
			var next *time.Time
			var err error
			if c.name == "reading a source" {
				next, err = h.store.NextSourceWorkAt(t.Context())
			} else {
				next, err = h.store.NextWorkAt(t.Context())
			}
			if err != nil {
				t.Fatal(err)
			}
			requireWithin(t, "next work", next, time.Time{}, time.Now().Add(5*time.Second))
			if c.name == "reading a source" {
				claim := h.sourceClaim(t)
				if err := h.store.ReleaseSource(t.Context(), claim); err != nil {
					t.Fatal(err)
				}
			}
			requireEqual(t, h.row(t, subscription.InboxID).deliveryError, (*string)(nil))
			requireEqual(t, h.subscription(t, subscription.ID).Failure, (*model.SourceFailure)(nil))
		})
	}
}

func TestAStopWhileTheInboxIsBeingGivenBackDoesNotKeepTheLease(t *testing.T) {
	h := newHarness(t, debounce(0, 300))
	subscription := h.subscribe(t, "first")
	h.postponeSources(t)
	h.addEvents(t, subscription, 1, 1)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	submitting, rowHeld := make(chan struct{}), make(chan struct{})
	h.dispatcher.respond = func(ctx context.Context, _ *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
		close(submitting)
		select {
		case <-rowHeld:
		case <-ctx.Done():
		}
		return dispatched(dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER, nil), nil
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = h.worker.ProcessOne(ctx)
	}()
	<-submitting
	// With the row held, the step has finished its work and waits in its release, where the stop arrives.
	holder, err := h.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer holder.Rollback()
	if _, err := holder.ExecContext(t.Context(), `SELECT 1 FROM inbox WHERE id = $1 FOR UPDATE`, subscription.InboxID); err != nil {
		t.Fatal(err)
	}
	close(rowHeld)
	h.awaitQueryWaitingForARow(t)
	cancel()
	if err := holder.Commit(); err != nil {
		t.Fatal(err)
	}
	<-done

	next, err := h.store.NextWorkAt(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	requireWithin(t, "next work", next, time.Time{}, time.Now().Add(5*time.Second))
}

func TestAReleaseThatFailsWhileStoppingIsLoggedByItsType(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	logs := captureLogs(t)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	h.actions.on(func(read context.Context, _ store.Claim, _ store.DueSubscription) error {
		// The stop arrives while the source is read, and the release that follows is refused.
		h.refuseInboxWrites(t)
		cancel()
		<-read.Done()
		return read.Err()
	})

	_, err := h.sourceWorker.ProcessOne(ctx)

	if !errors.Is(err, context.Canceled) {
		t.Fatalf("step = %v, want context.Canceled", err)
	}
	records := logs.records(t)
	if len(records) != 1 {
		t.Fatalf("%d log records, want 1: %v", len(records), records)
	}
	record := records[0]
	requireEqual(t, record["level"], "WARN")
	// The error's text is not logged: only the inbox and the kind of failure.
	delete(record, "time")
	delete(record, "level")
	delete(record, "msg")
	requireEqual(t, record, map[string]any{"inbox": subscription.InboxID.String(), "error_type": "*pq.Error"})
}

// awaitQueryWaitingForARow returns once a query waits for a row lock another transaction holds.
func (h *harness) awaitQueryWaitingForARow(t *testing.T) {
	t.Helper()
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	expired := time.After(awaitBound)
	for {
		var waiting bool
		if err := h.db.QueryRowContext(t.Context(), `
			SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock')`).
			Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		if waiting {
			return
		}
		select {
		case <-ticker.C:
		case <-expired:
			t.Fatal("no query waited for the inbox row")
		}
	}
}

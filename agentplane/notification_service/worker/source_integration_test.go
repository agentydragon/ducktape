package worker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc/status"
)

func TestProviderFailuresAreShownOnTheSubscriptionAndRetried(t *testing.T) {
	for _, c := range []struct {
		name    string
		failure error
		kind    store.SourceFailureKind
		message string
		retry   time.Duration
	}{
		{
			"default retry", &ProviderError{Kind: store.FailureUnavailable, Message: "Actions Service unavailable"},
			store.FailureUnavailable, "Actions Service unavailable", DefaultRetry,
		},
		{
			"retry the Provider asks for", &ProviderError{Kind: store.FailureRateLimited, Message: "rate limited", RetryAfter: 10 * time.Second},
			store.FailureRateLimited, "rate limited", 10 * time.Second,
		},
		{
			"quota", &store.QuotaError{Quota: store.EntriesPerInbox, Limit: 10},
			store.FailureProcessingError, "QuotaError", DefaultRetry,
		},
	} {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t, debounce(3600, 3600))
			subscription := h.subscribe(t, "first")
			h.actions.returns(c.failure)

			before := time.Now()
			h.step(t)
			after := time.Now()

			shown := h.subscription(t, subscription.ID)
			if shown.Failure == nil {
				t.Fatal("the subscription shows no failure")
			}
			requireEqual(t, [2]any{shown.Failure.Kind, shown.Failure.Message}, [2]any{c.kind, c.message})
			requireWithin(t, "retry", shown.RetryAt, before.Add(c.retry), after.Add(c.retry))
			// The destination is fine, so the inbox shows nothing wrong, and the source waits for its retry.
			requireEqual(t, h.row(t, subscription.InboxID).deliveryError, (*string)(nil))
			h.requireNoneDue(t)
		})
	}
}

func TestADeferredSourceShowsNoFailureAndWaitsForItsTime(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	until := time.Now().Add(2 * time.Hour).Truncate(time.Microsecond)
	h.actions.returns(&ProviderDeferred{Until: until})

	h.step(t)

	shown := h.subscription(t, subscription.ID)
	requireEqual(t, [2]any{shown.Failure, shown.RetryAt}, [2]any{(*store.SourceFailure)(nil), (*time.Time)(nil)})
	next, err := h.store.NextWorkAt(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	requireWithin(t, "next work", next, until, until)
	h.requireNoneDue(t)
}

func TestAProviderBreakingItsContractIsReturnedAfterTheInboxIsReleased(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	bug := errors.New("provider bug")
	h.actions.returns(bug)

	worked, err := h.worker.step(t.Context())

	var unclassified *UnclassifiedProviderError
	if !worked || !errors.As(err, &unclassified) || unclassified.SubscriptionID != subscription.ID || !errors.Is(err, bug) {
		t.Fatalf("step = %v, %v; want the Provider's error for subscription %s", worked, err, subscription.ID)
	}
	// It is not shown as a source failure, and the inbox is not held until its lease runs out.
	requireEqual(t, h.subscription(t, subscription.ID).Failure, (*store.SourceFailure)(nil))
	requireEqual(t, h.claim(t).ID, subscription.InboxID)
}

func TestAProviderWhoseClaimWasLostLeavesTheInboxToItsNewOwner(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	h.actions.returns(store.ErrClaimLost)

	h.step(t)

	requireEqual(t, h.subscription(t, subscription.ID).Failure, (*store.SourceFailure)(nil))
	// Nothing was released on the new owner's behalf.
	h.requireNoneDue(t)
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
				_, err := h.worker.step(ctx)
				done <- err
			}()
			<-started
			cancel()

			if err := <-done; !errors.Is(err, context.Canceled) {
				t.Fatalf("step = %v, want context.Canceled", err)
			}
			// The lease would hold the inbox for thirty seconds; its release schedules the next work soon.
			next, err := h.store.NextWorkAt(t.Context())
			if err != nil {
				t.Fatal(err)
			}
			requireWithin(t, "next work", next, time.Time{}, time.Now().Add(5*time.Second))
			requireEqual(t, h.row(t, subscription.InboxID).deliveryError, (*string)(nil))
			requireEqual(t, h.subscription(t, subscription.ID).Failure, (*store.SourceFailure)(nil))
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
		_, _ = h.worker.step(ctx)
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

	_, err := h.worker.step(ctx)

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

// insertGitHubSubscription adds a GitHub subscription to the inbox, as the GitHub stage's store will, with the
// rows it follows.
func (h *harness) insertGitHubSubscription(t *testing.T, inboxID uuid.UUID) uuid.UUID {
	t.Helper()
	repository := "owner/repository-" + uuid.NewString()
	creation, err := json.Marshal(store.Subscribe{
		DestinationRef: destination(), SessionID: testSession, IdempotencyKey: "github",
		Source: store.GitHubSource{Repository: repository, Subject: store.PullRequestSubject{Number: 1}}, LifetimeDays: 7,
	})
	if err != nil {
		t.Fatal(err)
	}
	h.exec(t, `INSERT INTO github_installation (app_id, installation_id, generation) VALUES (1, 2, 0) ON CONFLICT DO NOTHING`)
	var repositoryID int64
	if err := h.db.QueryRowContext(t.Context(),
		`INSERT INTO github_repository (full_name) VALUES ($1) RETURNING repository_id`, repository).Scan(&repositoryID); err != nil {
		t.Fatal(err)
	}
	h.exec(t, `INSERT INTO github_repository_access (app_id, installation_id, repository_id, generation) VALUES (1, 2, $1, 0)`, repositoryID)
	h.exec(t, `INSERT INTO github_subject (repository_id, kind, subject_key, generation) VALUES ($1, 'pull_request', '1', 0)`, repositoryID)
	id := uuid.New()
	h.exec(t, `ALTER TABLE subscription DROP CONSTRAINT subscription_creation_typed_required`)
	h.exec(t, `
		INSERT INTO subscription (id, inbox_id, idempotency_key, creation, version, github_start_position, generation,
			github_app_id, github_installation_id, github_repository_id, github_subject_kind, github_subject_key,
			cancelled, expires_at, next_attempt)
		VALUES ($1, $2, 'github', $3::jsonb, 1, 0, 0, 1, 2, $4, 'pull_request', '1', false, now() + interval '1 day', now())`,
		id, inboxID, string(creation), repositoryID)
	if err := store.BackfillSubscriptionCreations(t.Context(), h.db); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestGitHubSubscriptionsAreReadByTheGitHubProvider(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		t.Run(fmt.Sprintf("provider enabled=%v", enabled), func(t *testing.T) {
			h := newIsolatedHarness(t, debounce(3600, 3600))
			actions := h.subscribe(t, "first")
			h.postponeSources(t)
			github := h.insertGitHubSubscription(t, actions.InboxID)
			provider := newFakeProvider()
			if enabled {
				h.worker.GitHub = provider
			}

			h.step(t)

			requireEqual(t, h.actions.callCount(), 0)
			if enabled {
				if len(provider.calls) != 1 {
					t.Fatalf("%d GitHub reads, want 1", len(provider.calls))
				}
				if _, ok := provider.calls[0].Source.(store.GitHubSource); !ok || provider.calls[0].ID != github {
					t.Errorf("read %+v, want the GitHub subscription", provider.calls[0].Subscription)
				}
				return
			}
			// A disabled provider is a source that is unavailable, which the subscription shows and retries.
			shown := h.subscription(t, github)
			if shown.Failure == nil || shown.Failure.Kind != store.FailureUnavailable {
				t.Errorf("failure = %+v, want an unavailable source", shown.Failure)
			}
		})
	}
}

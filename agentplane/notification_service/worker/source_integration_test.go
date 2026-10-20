package worker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/uuid"
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

func TestShuttingDownLeavesTheLeaseToRunOut(t *testing.T) {
	h := newHarness(t, debounce(3600, 3600))
	subscription := h.subscribe(t, "first")
	started := make(chan struct{})
	h.actions.on(func(ctx context.Context, _ store.Claim, _ store.DueSubscription) error {
		close(started)
		<-ctx.Done()
		return ctx.Err()
	})
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	begun := time.Now()
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
	h.requireNoneDue(t)
	next, err := h.store.NextWorkAt(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	requireWithin(t, "next work", next, begun.Add(store.LeaseDuration), time.Now().Add(store.LeaseDuration))
	requireEqual(t, h.subscription(t, subscription.ID).Failure, (*store.SourceFailure)(nil))
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
	h.exec(t, `
		INSERT INTO subscription (id, inbox_id, idempotency_key, creation, version, github_start_position, generation,
			github_app_id, github_installation_id, github_repository_id, github_subject_kind, github_subject_key,
			cancelled, expires_at, next_attempt)
		VALUES ($1, $2, 'github', $3::jsonb, 1, 0, 0, 1, 2, $4, 'pull_request', '1', false, now() + interval '1 day', now())`,
		id, inboxID, string(creation), repositoryID)
	return id
}

func TestGitHubSubscriptionsAreReadByTheGitHubProvider(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		t.Run(fmt.Sprintf("provider enabled=%v", enabled), func(t *testing.T) {
			h := newHarness(t, debounce(3600, 3600))
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

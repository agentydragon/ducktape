package github

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/client"
	"github.com/agentydragon/ducktape/agentplane/notification_service/github/settings"
	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/github/testing" // Shared fake API server; avoids colliding with Go testing.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
)

func merged(base, extra map[string]any) map[string]any {
	out := map[string]any{}
	for _, fields := range []map[string]any{base, extra} {
		for key, value := range fields {
			out[key] = value
		}
	}
	return out
}

func TestOverlappingSubscriptionsShareOneEntryAndStopWhenAccessIsRevoked(t *testing.T) {
	h := newHarness(t)
	// Before the subscriptions: not a historical replay.
	h.ingest("issue_comment", comment(7))
	first := h.follow(pullRequestSource(7), "follow")
	second := h.follow(pullRequestSource(7), "overlap")
	h.ingest("issue_comment", comment(8))
	h.ingest("issue_comment", comment(7))
	claim := h.claim()
	h.reconcileAll(claim)

	page := h.page(first.InboxID)
	if len(page.Entries) != 1 {
		t.Fatalf("%d entries, want the one comment on pull request 7 after the subscriptions", len(page.Entries))
	}
	requireEqual(t, page.Entries[0].SubscriptionIDs, sortedIDs(first.ID, second.ID))
	requireEqual(t, payloads(t, page.Entries), []any{asJSON(t, comment(7))})

	h.ingest("issue_comment", comment(7))
	h.world.update(func(w *world) { w.revoked = true })
	h.ingest("installation", installationEvent("suspend"))
	fixture.AsError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, h.next(claim)))
	after := h.page(first.InboxID)
	requireEqual(t, after.Entries, page.Entries)
	requireEqual(t, after.Inbox, page.Inbox)
}

func TestARateLimitIsASharedBackoffThatSurvivesIngressAndARestart(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7), "follow")
	h.ingest("issue_comment", comment(7))
	h.world.update(func(w *world) { w.limited = true })
	claim := h.claim()
	due := h.next(claim)

	before := time.Now()
	retryAt := fixture.AsError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, due)).Until
	if retryAt.Before(before.Add(120*time.Second)) || retryAt.After(time.Now().Add(120*time.Second)) {
		t.Fatalf("retry at %v, want 120s after the limit", retryAt)
	}
	if err := h.store.SourceDeferred(t.Context(), claim, due.Subscription, retryAt); err != nil {
		t.Fatal(err)
	}

	// The failure is the shared access row's; the subscription shows none of its own.
	view := h.subscription(subscription.ID)
	if view.Failure != nil || view.RetryAt != nil || view.GitHub == nil {
		t.Fatalf("subscription = %+v, want no failure of its own", view)
	}
	access := view.GitHub.Access[0]
	if access.Failure == nil || access.Failure.Kind != store.FailureRateLimited || access.RetryAt == nil || !access.RetryAt.Equal(retryAt) {
		t.Fatalf("access = %+v, want a rate limit retried at %v", access, retryAt)
	}
	if page := h.page(subscription.InboxID); len(page.Entries) != 0 || page.Inbox.LastCursor != 0 || page.Notice != nil {
		t.Fatalf("page = %+v, want nothing", page)
	}

	// Ingress wakes the source, and the backoff holds without asking GitHub.
	requests := h.fake.Total()
	h.ingest("issue_comment", comment(7))
	waiting := fixture.AsError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, h.next(claim)))
	if !waiting.Until.Equal(retryAt) || h.fake.Total() != requests {
		t.Fatalf("deferred until %v after %d new requests, want %v after none", waiting.Until, h.fake.Total()-requests, retryAt)
	}

	// The deadline passes, and a new process recovers from the rows alone.
	h.exec(`UPDATE github_repository_access SET next_attempt = now()`)
	h.exec(`UPDATE subscription SET next_attempt = now()`)
	h.world.update(func(w *world) { w.limited = false })
	h.restart()
	h.reconcileAll(claim)
	view = h.subscription(subscription.ID)
	if view.Failure != nil || view.LastSuccessAt == nil || !view.GitHub.Access[0].CurrentlyValid {
		t.Fatalf("subscription = %+v, want it recovered", view)
	}
	page := h.page(subscription.InboxID)
	requireEqual(t, payloads(t, page.Entries), []any{asJSON(t, comment(7)), asJSON(t, comment(7))})
}

func TestSharedRefreshesAreMadeOnceAndSurviveARestart(t *testing.T) {
	h := newHarness(t)
	first := h.follow(pullRequestSource(7), "follow")
	second := h.follow(pullRequestSource(7), "overlap")
	h.follow(pullRequestSource(8), "another")
	h.fake.Forget()
	claim := h.claim()
	h.reconcileAll(claim)
	for path, want := range map[string]int{
		"/repos/owner/repo/installation":                          1,
		"/repositories/100":                                       1,
		"/repos/owner/repo/pulls/7":                               1,
		"/repos/owner/repo/pulls/8":                               1,
		"/repos/owner/repo/git/commits/" + fixture.MergeCommitSHA: 2,
		"/repos/owner/repo/git/ref/heads/main":                    2,
	} {
		if got := h.fake.Count(path); got != want {
			t.Errorf("%d requests for %s, want %d", got, path, want)
		}
	}

	requests := h.fake.Total()
	h.restart()
	h.ingest("issue_comment", comment(7))
	h.reconcileAll(claim)
	if h.fake.Total() != requests {
		t.Errorf("%d requests after a restart, want none", h.fake.Total()-requests)
	}
	page := h.page(first.InboxID)
	if len(page.Entries) != 1 {
		t.Fatalf("%d entries, want 1", len(page.Entries))
	}
	requireEqual(t, page.Entries[0].SubscriptionIDs, sortedIDs(first.ID, second.ID))
}

func TestUnknownMergeabilityStillDeliversAndSchedulesARepair(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7), "follow")
	h.fake.Respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
		`{"number":7,"state":"open","merged":false,"base":{"ref":"main","sha":"`+fixture.BaseSHA+`"},"mergeable":null,"head":{"sha":"`+fixture.SHA+`","repo":{"id":200,"full_name":"fork/repo"}}}`)
	h.ingest("issue_comment", comment(7))
	claim := h.claim()
	h.reconcileAll(claim)

	if entries := h.page(subscription.InboxID).Entries; len(entries) != 1 {
		t.Fatalf("%d entries with mergeability unknown, want the webhook delivered", len(entries))
	}
	subject, err := store.NewGitHubSubjectKey(baseRepository, model.PullRequestSubject{Number: 7})
	if err != nil {
		t.Fatal(err)
	}
	state, err := h.provider.state.Subject(t.Context(), subject)
	if err != nil {
		t.Fatal(err)
	}
	freshness := time.Duration(settings.DefaultSettings().FreshnessSeconds) * time.Second
	if state.LastSuccessAt == nil || !state.RepairAt.Before(state.LastSuccessAt.Add(freshness)) {
		t.Fatalf("subject state = %+v, want a persisted repair before normal freshness expiry", state)
	}
}

func TestAnInitialConflictProducesASyntheticMergeabilityEntry(t *testing.T) {
	h := newHarness(t)
	source := pullRequestSource(7, model.EventFilter{Event: model.EventMergeabilityChanged})
	subscription := h.follow(source, "follow")
	h.fake.Respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
		`{"number":7,"state":"open","merged":false,"base":{"ref":"main","sha":"`+fixture.BaseSHA+`"},"mergeable":false,"head":{"sha":"`+fixture.SHA+`","repo":{"id":200,"full_name":"fork/repo"}}}`)
	h.reconcileAll(h.claim())

	entries := h.page(subscription.InboxID).Entries
	if len(entries) != 1 {
		t.Fatalf("%d entries, want the initial conflict observation", len(entries))
	}
	event, ok := entries[0].Event.(model.GitHubMergeabilityEvent)
	if !ok || event.AppID != fixture.AppID || event.RepositoryID != baseRepository || event.PullRequestNumber != 7 || event.Sequence <= 0 {
		t.Fatalf("event = %#v, want the pull request's synthetic mergeability identity", entries[0].Event)
	}
	var payload struct {
		Repository           string                     `json:"repository"`
		PullRequestNumber    int64                      `json:"pullRequestNumber"`
		BaseRef              string                     `json:"baseRef"`
		HeadSHA              string                     `json:"headSha"`
		BaseSHA              string                     `json:"baseSha"`
		PreviousMergeability model.GitHubMergeableState `json:"previousMergeability"`
		Mergeability         model.GitHubMergeableState `json:"mergeability"`
		ObservedAt           time.Time                  `json:"observedAt"`
	}
	if err := json.Unmarshal(entries[0].Payload, &payload); err != nil {
		t.Fatal(err)
	}
	if payload.Repository != baseRepositoryName || payload.PullRequestNumber != 7 || payload.BaseRef != "main" ||
		payload.HeadSHA != fixture.SHA || payload.BaseSHA != fixture.BaseSHA || payload.PreviousMergeability != model.GitHubMergeabilityUnknown ||
		payload.Mergeability != model.GitHubConflicting || payload.ObservedAt.IsZero() {
		t.Errorf("payload = %+v, want the initial conflict state observed from GitHub", payload)
	}
}

func TestUnverifiedMergeabilityStaysUnknownUntilTheTestMergeMatchesCurrentBase(t *testing.T) {
	h := newHarness(t)
	source := pullRequestSource(7, model.EventFilter{Event: model.EventMergeabilityChanged})
	subscription := h.follow(source, "follow")
	pullRequestPath := "/repos/owner/repo/pulls/7"
	commitPath := "/repos/owner/repo/git/commits/" + fixture.MergeCommitSHA
	h.fake.Respond(pullRequestPath, http.StatusOK, nil,
		`{"number":7,"state":"open","merged":false,"base":{"ref":"main","sha":"`+fixture.BaseSHA+`"},"mergeable":false,"head":{"sha":"`+fixture.SHA+`","repo":{"id":100,"full_name":"owner/repo"}}}`)
	claim := h.claim()
	h.reconcileAll(claim)
	initialEntries := h.page(subscription.InboxID).Entries
	if len(initialEntries) != 1 {
		t.Fatalf("%d entries after the initial conflict, want its transition", len(initialEntries))
	}
	initialEvent, ok := initialEntries[0].Event.(model.GitHubMergeabilityEvent)
	if !ok || initialEvent.Sequence <= 0 {
		t.Fatalf("initial event identity = %#v, want sequenced transition", initialEntries[0].Event)
	}
	if got := h.text(`SELECT mergeability::text FROM github_pull_request WHERE repository_id = $1 AND number = 7`, baseRepository); got != "CONFLICTING" {
		t.Fatalf("persisted mergeability after the initial observation = %q, want CONFLICTING", got)
	}
	h.fake.Restore(pullRequestPath)
	h.world.update(func(w *world) { w.base = fixture.OtherBaseSHA })
	// GitHub says the PR is mergeable, but its test merge still has the previous base as a parent.
	h.fake.Respond(commitPath, http.StatusOK, nil, fixture.MergeCommitBody(fixture.MergeCommitSHA, fixture.BaseSHA, fixture.SHA))
	h.ingest("pull_request", pullRequestPayload(fixture.SHA))
	h.reconcileAll(claim)

	if got := h.text(`SELECT mergeability::text FROM github_pull_request WHERE repository_id = $1 AND number = 7`, baseRepository); got != "UNKNOWN" {
		t.Fatalf("persisted mergeability = %q, want UNKNOWN while the test merge has a stale base parent", got)
	}
	if got := h.text(`SELECT base_sha FROM github_pull_request WHERE repository_id = $1 AND number = 7`, baseRepository); got != fixture.OtherBaseSHA {
		t.Fatalf("persisted base SHA = %q, want current branch tip %s", got, fixture.OtherBaseSHA)
	}
	state, err := h.provider.state.Subject(t.Context(), store.GitHubSubjectKey{RepositoryID: baseRepository, Kind: "pull_request", Key: "7"})
	if err != nil {
		t.Fatal(err)
	}
	freshness := time.Duration(settings.DefaultSettings().FreshnessSeconds) * time.Second
	if state.LastSuccessAt == nil || !state.RepairAt.Before(state.LastSuccessAt.Add(freshness)) {
		t.Fatalf("subject state = %+v, want a durable early retry for UNKNOWN", state)
	}
	if entries := h.page(subscription.InboxID).Entries; len(entries) != 1 {
		t.Fatalf("%d entries after the unverified positive, want no resolution notice", len(entries))
	}

	// A later poll gets a test merge whose parents match the current base and head. The PR's base.sha is stale,
	// so the proof must be checked against the ref read by the client.
	h.fake.Restore(commitPath)
	h.fake.Respond(pullRequestPath, http.StatusOK, nil,
		`{"number":7,"state":"open","merged":false,"base":{"ref":"main","sha":"`+fixture.BaseSHA+`"},"mergeable":true,"merge_commit_sha":"`+fixture.MergeCommitSHA+`","head":{"sha":"`+fixture.SHA+`","repo":{"id":100,"full_name":"owner/repo"}}}`)
	h.exec(`UPDATE github_subject SET next_attempt = now() WHERE repository_id = $1 AND kind = 'pull_request' AND subject_key = '7'`, baseRepository)
	h.exec(`UPDATE subscription SET next_attempt = now() WHERE id = $1`, subscription.ID)
	h.exec(`UPDATE inbox SET next_attempt = now() WHERE id = $1`, subscription.InboxID)
	h.fake.Forget()
	h.restart()
	if got := h.text(`SELECT mergeability::text FROM github_pull_request WHERE repository_id = $1 AND number = 7`, baseRepository); got != "UNKNOWN" {
		t.Fatalf("persisted mergeability after restart = %q, want UNKNOWN before the fresh poll", got)
	}
	h.reconcileAll(claim)

	if got := h.text(`SELECT mergeability::text FROM github_pull_request WHERE repository_id = $1 AND number = 7`, baseRepository); got != "MERGEABLE" {
		t.Fatalf("persisted mergeability after restart = %q, want MERGEABLE", got)
	}
	if h.fake.Count("/repos/owner/repo/git/ref/heads/main") != 1 || h.fake.Count(commitPath) != 1 {
		t.Errorf("current-base ref reads = %d, test-merge reads = %d, want one each",
			h.fake.Count("/repos/owner/repo/git/ref/heads/main"), h.fake.Count(commitPath))
	}
	entries := h.page(subscription.InboxID).Entries
	if len(entries) != 2 {
		t.Fatalf("%d entries after the matching proof, want conflict and resolution transitions", len(entries))
	}
	if event, ok := entries[1].Event.(model.GitHubMergeabilityEvent); !ok || event.Sequence <= initialEvent.Sequence {
		t.Fatalf("resolution event identity = %#v, want sequenced transition", entries[1].Event)
	}
	var notice struct {
		PreviousMergeability model.GitHubMergeableState `json:"previousMergeability"`
		Mergeability         model.GitHubMergeableState `json:"mergeability"`
		BaseSHA              string                     `json:"baseSha"`
		HeadSHA              string                     `json:"headSha"`
		MergeCommit          *model.GitHubMergeCommit   `json:"mergeCommit"`
	}
	if err := json.Unmarshal(entries[1].Payload, &notice); err != nil {
		t.Fatal(err)
	}
	if notice.PreviousMergeability != model.GitHubConflicting || notice.Mergeability != model.GitHubMergeable ||
		notice.BaseSHA != fixture.OtherBaseSHA || notice.HeadSHA != fixture.SHA || notice.MergeCommit == nil ||
		*notice.MergeCommit != (model.GitHubMergeCommit{SHA: fixture.MergeCommitSHA, BaseSHA: fixture.OtherBaseSHA, HeadSHA: fixture.SHA}) {
		t.Errorf("resolution payload = %+v, want a mergeable notice proved against current refs", notice)
	}
}

// The access is validated before the subject is read, so it is the first thing a page was made under to go stale.
func TestASubscriptionIsReadAgainWhenTheEarliestThingItWasReadUnderGoesStale(t *testing.T) {
	h := newHarness(t)
	h.follow(pullRequestSource(7), "follow")
	h.reconcile(h.claim())

	staleAt := h.count(`
		SELECT count(*) FROM subscription s, github_repository_access a, github_subject g
		WHERE s.next_attempt = a.valid_until AND a.valid_until < g.last_success_at + make_interval(secs => $1)`,
		float64(settings.DefaultSettings().FreshnessSeconds))
	if staleAt != 1 {
		t.Error("the subscription is not due again exactly when the access it was read under goes stale")
	}
}

func TestSharedErrorsAreProjectedWithoutBeingCopiedIntoSubscriptions(t *testing.T) {
	h := newHarness(t)
	first := h.follow(pullRequestSource(7), "follow")
	second := h.follow(pullRequestSource(7), "other")
	h.world.update(func(w *world) { w.limited = true })
	claim := h.claim()
	for range 2 {
		due := h.next(claim)
		deferred := fixture.AsError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, due))
		if err := h.store.SourceDeferred(t.Context(), claim, due.Subscription, deferred.Until); err != nil {
			t.Fatal(err)
		}
	}

	views := []store.Subscription{h.subscription(first.ID), h.subscription(second.ID)}
	requireEqual(t, views[0].GitHub, views[1].GitHub)
	if failure := views[0].GitHub.Access[0].Failure; failure == nil || failure.Kind != store.FailureRateLimited {
		t.Errorf("access failure = %+v, want a rate limit", failure)
	}
	for _, view := range views {
		if view.Failure != nil || view.LastSuccessAt != nil {
			t.Errorf("subscription %s = %+v, want no failure and no success of its own", view.ID, view)
		}
	}
	if rows := h.count(`SELECT count(*) FROM github_repository_access`); rows != 1 {
		t.Errorf("%d access rows, want 1", rows)
	}
	page := h.page(first.InboxID)
	if len(page.Entries) != 0 || page.Inbox.LastCursor != 0 || page.Inbox.Acknowledged != 0 || page.Notice != nil {
		t.Errorf("page = %+v, want nothing", page)
	}
}

func TestARefreshThatFailsIsRecordedOnItsSharedRowByKind(t *testing.T) {
	for name, scenario := range map[string]struct {
		fail func(*harness)
		kind store.SourceFailureKind
	}{
		"GitHub failing": {
			func(h *harness) { h.fake.Respond("/repositories/100", http.StatusBadGateway, nil, "") }, store.FailureUnavailable,
		},
		"a request GitHub refuses": {
			func(h *harness) { h.fake.Respond("/repositories/100", http.StatusUnprocessableEntity, nil, "") }, store.FailureProcessingError,
		},
		"a suspended installation": {
			func(h *harness) {
				h.fake.Respond("/repos/owner/repo/installation", http.StatusOK, nil, `{"id": 11, "suspended_at": "2026-01-01T00:00:00Z"}`)
			}, store.FailureAccessDenied,
		},
		"access revoked": {
			func(h *harness) { h.world.update(func(w *world) { w.revoked = true }) }, store.FailureAccessDenied,
		},
		"a repository without its name": {
			func(h *harness) { h.fake.Respond("/repositories/100", http.StatusOK, nil, `{"id": 100}`) }, store.FailureProcessingError,
		},
	} {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t)
			subscription := h.follow(pullRequestSource(7), "follow")
			scenario.fail(h)
			claim := h.claim()
			due := h.next(claim)

			before := time.Now()
			deferred := fixture.AsError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, due))
			if deferred.Until.Before(before.Add(worker.DefaultRetry)) || deferred.Until.After(time.Now().Add(worker.DefaultRetry)) {
				t.Errorf("deferred until %v, want %v after the failure", deferred.Until, worker.DefaultRetry)
			}
			view := h.subscription(subscription.ID)
			if view.Failure != nil {
				t.Errorf("subscription failure = %+v, want the shared row's alone", view.Failure)
			}
			if recorded := view.GitHub.Access[0].Failure; recorded == nil || recorded.Kind != scenario.kind {
				t.Errorf("access failure = %+v, want kind %s", recorded, scenario.kind)
			}

			requests := h.fake.Total()
			fixture.AsError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, due))
			if h.fake.Total() != requests {
				t.Error("GitHub was asked again during the backoff")
			}
		})
	}
}

func TestACallerThatGoesAwayDoesNotFailTheSharedRow(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7), "follow")
	entered := make(chan struct{})
	var once sync.Once
	h.world.update(func(w *world) {
		w.onRequest = func(ctx context.Context, path string) {
			if path == "/repositories/100" {
				once.Do(func() { close(entered) })
				<-ctx.Done()
			}
		}
	})
	claim := h.claim()
	due := h.next(claim)

	ctx, cancel := context.WithCancel(t.Context())
	result := make(chan error, 1)
	go func() { result <- h.provider.Reconcile(ctx, claim, due) }()
	<-entered
	cancel()
	if err := <-result; !errors.Is(err, context.Canceled) {
		t.Fatalf("error = %v, want the caller's cancellation", err)
	}
	// The caller learned nothing about GitHub, so the row is neither failed nor valid; its lease runs out.
	access := h.subscription(subscription.ID).GitHub.Access[0]
	if access.Failure != nil || access.CurrentlyValid || access.RefreshingUntil == nil {
		t.Errorf("access = %+v, want an unfinished refresh without a failure", access)
	}
}

func TestAnInvalidationDuringARefreshFencesIt(t *testing.T) {
	for _, event := range []string{"installation", "installation_repositories"} {
		t.Run(event, func(t *testing.T) {
			h := newHarness(t)
			subscription := h.follow(pullRequestSource(7), "follow")
			var once sync.Once
			h.world.update(func(w *world) {
				w.onRequest = func(_ context.Context, path string) {
					if path == "/repositories/100" {
						once.Do(func() {
							if err := h.deliver(event, installationEvent("removed")); err != nil {
								t.Error(err)
							}
						})
					}
				}
			})
			claim := h.claim()
			fixture.AsError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, h.next(claim)))
			if h.subscription(subscription.ID).GitHub.Access[0].CurrentlyValid {
				t.Fatal("a refresh read before the invalidation was recorded")
			}

			// Nothing was kept of the interrupted refresh: the next read refreshes the access afresh.
			h.reconcile(claim)
			if !h.subscription(subscription.ID).GitHub.Access[0].CurrentlyValid {
				t.Error("the access was not validated after the invalidation")
			}
		})
	}
}

func TestTheRepositoryIsReadByItsNumericIdentityAfterARename(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7), "follow")
	h.fake.Respond("/repositories/100", http.StatusOK, nil, `{"id": 100, "full_name": "owner/renamed"}`)
	h.reconcile(h.claim())

	for _, path := range []string{"/repos/owner/renamed/installation", "/repos/owner/renamed/pulls/7"} {
		if h.fake.Count(path) != 1 {
			t.Errorf("%d requests for %s, want 1", h.fake.Count(path), path)
		}
	}
	if name := h.text(`SELECT full_name FROM github_repository WHERE repository_id = 100`); name != "owner/renamed" {
		t.Errorf("repository name = %q, want the one GitHub reports", name)
	}
	view := h.subscription(subscription.ID)
	if source := view.Source.(model.GitHubSource); source.Repository != baseRepositoryName {
		t.Errorf("subscription repository = %q, want the one it was created with", source.Repository)
	}
	if !view.GitHub.Access[0].CurrentlyValid {
		t.Error("the renamed repository's access is not valid")
	}
}

func TestAnAccessIsRefreshedByOneWorkerAtATime(t *testing.T) {
	h := newHarness(t)
	h.follow(pullRequestSource(7), "follow")
	entered, resume := make(chan struct{}), make(chan struct{})
	var once sync.Once
	h.world.update(func(w *world) {
		w.onRequest = func(ctx context.Context, path string) {
			if path == "/repositories/100" {
				once.Do(func() { close(entered) })
				select {
				case <-resume:
				case <-ctx.Done():
				}
			}
		}
	})
	h.fake.Forget()
	slow := h.provider
	var fence store.GitHubAccessFence
	result := make(chan error, 1)
	go func() {
		var err error
		fence, err = slow.refreshAccess(t.Context(), baseAccess)
		result <- err
	}()
	<-entered

	h.restart()
	_, err := h.provider.refreshAccess(t.Context(), baseAccess)
	fixture.AsError[*store.RefreshDeferredError](t, err)
	close(resume)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	requireEqual(t, fence.Key, baseAccess)
	if h.fake.Count("/repositories/100") != 1 || h.fake.Count("/repos/owner/repo/installation") != 1 {
		t.Errorf("%d reads of the repository and %d of its installation, want one each",
			h.fake.Count("/repositories/100"), h.fake.Count("/repos/owner/repo/installation"))
	}
}

func TestAForkDeliveryIsOwedOnlyUnderTheForksCurrentInstallationAccess(t *testing.T) {
	h := newHarness(t)
	h.world.update(func(w *world) { w.fork = true })
	subscription := h.follow(pullRequestSource(7), "follow")
	inFork := checkInFork(nextSHA, forkInstallation)
	h.ingest("check_run", inFork)
	// The same commit under an unrelated installation is never owed.
	h.ingest("check_run", checkInFork(nextSHA, otherInstallation))
	h.world.update(func(w *world) { w.head = nextSHA })
	h.fake.Respond("/repos/fork/repo/installation", http.StatusNotFound, nil, "")
	claim := h.claim()
	h.reconcile(claim)
	if entries := h.page(subscription.InboxID).Entries; len(entries) != 0 {
		t.Fatalf("%d entries while the App is not installed on the fork, want none", len(entries))
	}

	// A fork that was not covered is found when the subject's refresh comes due.
	h.fake.Restore("/repos/fork/repo/installation")
	h.exec(`UPDATE github_subject SET last_success_at = now() - interval '1 hour'`)
	h.ingest("issue_comment", comment(7))
	h.reconcileAll(claim)
	requireEqual(t, payloads(t, h.page(subscription.InboxID).Entries), []any{asJSON(t, inFork), asJSON(t, comment(7))})
	if grants := h.count(`SELECT count(*) FROM github_repository_access WHERE repository_id = $1`, forkRepository); grants != 1 {
		t.Errorf("%d grants on the fork, want 1", grants)
	}
}

func TestAWebhookRacingTheBootstrapFencesTheReadAndKeepsBothRevisionAssociations(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7, model.EventFilter{Event: model.EventCheckRun}), "follow")
	h.ingest("check_run", check(nextSHA))
	h.ingest("check_run", check(fixture.SHA))
	var once sync.Once
	h.world.update(func(w *world) {
		w.onRequest = func(_ context.Context, path string) {
			if strings.HasSuffix(path, "/pulls/7") {
				once.Do(func() {
					if err := h.deliver("pull_request", pullRequestPayload(nextSHA)); err != nil {
						t.Error(err)
					}
				})
			}
		}
	})
	claim := h.claim()
	fixture.AsError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, h.next(claim)))
	h.reconcileAll(claim)
	requireEqual(t, payloads(t, h.page(subscription.InboxID).Entries), []any{asJSON(t, check(nextSHA)), asJSON(t, check(fixture.SHA))})

	// A delayed older observation cannot erase the newer one.
	requests := h.fake.Count("/repos/owner/repo/pulls/7")
	h.ingest("pull_request", pullRequestPayload(fixture.SHA))
	h.ingest("check_run", check(nextSHA))
	h.reconcileAll(claim)
	if got := h.fake.Count("/repos/owner/repo/pulls/7") - requests; got != 1 {
		t.Errorf("%d PR reads, want one after the PR webhook invalidates the subject", got)
	}
	if cursor := h.page(subscription.InboxID).Inbox.LastCursor; cursor != 3 {
		t.Errorf("last cursor = %d, want 3", cursor)
	}
}

func TestBranchActivityAndAFixedCommit(t *testing.T) {
	for kind, want := range map[string]int{"branch": 3, "commit": 1} {
		t.Run(kind, func(t *testing.T) {
			h := newHarness(t)
			var subject model.GitHubSubject = model.BranchSubject{Name: "devel"}
			if kind == "commit" {
				subject = model.CommitSubject{SHA: fixture.SHA}
			}
			subscription := h.follow(model.GitHubSource{Repository: baseRepositoryName, Subject: subject}, "follow")
			base := map[string]any{
				"installation": map[string]any{"id": baseInstallation},
				"repository":   repositoryPayload(baseRepository, baseRepositoryName),
			}
			h.ingest("push", merged(base, map[string]any{"ref": "refs/heads/devel", "after": fixture.SHA}))
			h.world.update(func(w *world) { w.head = nextSHA })
			h.ingest("check_run", check(fixture.SHA))
			h.ingest("delete", merged(base, map[string]any{"ref": "devel", "ref_type": "branch"}))
			if kind == "branch" {
				h.world.update(func(w *world) { w.head = "" })
			} else {
				// A fixed commit that does not match cannot become a match later.
				h.ingest("check_run", check(nextSHA))
			}
			h.reconcile(h.claim())
			if entries := h.page(subscription.InboxID).Entries; len(entries) != want {
				t.Errorf("%d entries, want %d", len(entries), want)
			}
		})
	}
}

func TestAMatchingPageIsNotCappedByTheHistoryOfAssociations(t *testing.T) {
	h := newHarness(t)
	source := pullRequestSource(7, model.EventFilter{Event: model.EventCheckRun, Actions: []string{"completed"}})
	// Association history that is older than the subscription and longer than a page.
	for i := range 130 {
		h.ingest("pull_request", pullRequestPayload(fmt.Sprintf("%040x", i)))
	}
	subscription := h.follow(source, "follow")
	sha := fmt.Sprintf("%040x", 1)
	created := check(sha)
	created["action"] = "created"
	h.ingest("check_run", created)
	for range 130 {
		h.ingest("check_run", check(sha))
	}
	claim := h.claim()
	h.reconcile(claim)
	if cursor := h.page(subscription.InboxID).Inbox.LastCursor; cursor != store.GitHubDeliveryPageSize {
		t.Fatalf("last cursor = %d after the first page, want %d", cursor, store.GitHubDeliveryPageSize)
	}

	// A full page makes the subscription due again at once.
	h.reconcile(claim)
	page, err := h.store.Read(t.Context(), testIncarnation, subscription.InboxID, store.GitHubDeliveryPageSize, store.GitHubDeliveryPageSize)
	if err != nil || len(page.Entries) != 2 || page.Inbox.LastCursor != 130 {
		t.Fatalf("second page = %+v, %v; want the last 2 of 130 entries", page, err)
	}
	h.requireNoneDue(claim)
}

func TestASubscriptionOfAnotherAppIsASourceChange(t *testing.T) {
	h := newHarness(t)
	h.follow(pullRequestSource(7), "follow")
	claim := h.claim()
	due := h.next(claim)
	settings := settings.DefaultSettings()
	settings.AppID = fixture.AppID + 1
	other := NewProvider(h.provider.client, h.store, settings)

	requests := h.fake.Total()
	failure := fixture.AsError[*worker.ProviderError](t, other.Reconcile(t.Context(), claim, due))
	if failure.Kind != store.FailureSourceChanged || h.fake.Total() != requests {
		t.Errorf("failure = %+v after %d new requests, want a source change without asking GitHub", failure, h.fake.Total()-requests)
	}
}

func TestAuthorizeReturnsTheAccessASourceIsReadUnder(t *testing.T) {
	h := newHarness(t)
	for name, subject := range map[string]model.GitHubSubject{
		"a pull request": model.PullRequestSubject{Number: 7},
		"an issue":       model.IssueSubject{Number: 8},
		"a branch":       model.BranchSubject{Name: "feature/x"},
		"a commit":       model.CommitSubject{SHA: fixture.SHA},
	} {
		access, err := h.provider.Authorize(t.Context(), model.GitHubSource{Repository: baseRepositoryName, Subject: subject})
		if err != nil || access != baseAccess {
			t.Errorf("%s: Authorize = %+v, %v; want %+v", name, access, err, baseAccess)
		}
	}
	// A branch that does not exist yet can be followed.
	h.world.update(func(w *world) { w.head = "" })
	source := model.GitHubSource{Repository: baseRepositoryName, Subject: model.BranchSubject{Name: "later"}}
	if access, err := h.provider.Authorize(t.Context(), source); err != nil || access != baseAccess {
		t.Errorf("a branch that does not exist: Authorize = %+v, %v", access, err)
	}
}

func TestAuthorizeFailsWithTheRateLimitAndItsDelay(t *testing.T) {
	h := newHarness(t)
	h.world.update(func(w *world) { w.limited = true })
	_, err := h.provider.Authorize(t.Context(), pullRequestSource(7))
	if limited := fixture.AsError[*client.RateLimitedError](t, err); limited.RetrySeconds != 120 {
		t.Errorf("retry seconds = %d, want the 120 GitHub asked for", limited.RetrySeconds)
	}
}

func TestForkAccessFailuresAreNotTreatedAsUninstalled(t *testing.T) {
	for _, failure := range []struct {
		path   string
		status int
	}{
		{"/repos/fork/repo/installation", http.StatusUnauthorized},
		{"/repos/fork/repo/installation", http.StatusForbidden},
		{"/app/installations/22/access_tokens", http.StatusUnauthorized},
		{"/app/installations/22/access_tokens", http.StatusForbidden},
		{"/app/installations/22/access_tokens", http.StatusNotFound},
		{"/repos/fork/repo", http.StatusNotFound},
	} {
		t.Run(fmt.Sprintf("%s %d", failure.path, failure.status), func(t *testing.T) {
			h := newHarness(t)
			h.world.update(func(w *world) { w.fork = true })
			h.fake.Respond(failure.path, failure.status, nil, "")
			_, err := h.provider.Authorize(t.Context(), pullRequestSource(7))
			if denied := fixture.AsError[*client.AccessError](t, err); denied.StatusCode != failure.status {
				t.Errorf("access error = %+v, want status %d", denied, failure.status)
			}
			var notInstalled *client.NotInstalledError
			if errors.As(err, &notInstalled) {
				t.Error("an access failure was taken for an uninstalled fork")
			}
		})
	}
}

func TestAnUninstalledForkIsOptionalButASuspendedOneIsAnError(t *testing.T) {
	h := newHarness(t)
	h.world.update(func(w *world) { w.fork = true })
	h.fake.Respond("/repos/fork/repo/installation", http.StatusNotFound, nil, "")
	if access, err := h.provider.Authorize(t.Context(), pullRequestSource(7)); err != nil || access != baseAccess {
		t.Fatalf("Authorize with the fork uninstalled = %+v, %v", access, err)
	}
	h.fake.Respond("/repos/fork/repo/installation", http.StatusOK, nil, `{"id": 22, "suspended_at": "2026-01-01T00:00:00Z"}`)
	_, err := h.provider.Authorize(t.Context(), pullRequestSource(7))
	fixture.AsError[*client.SuspendedError](t, err)
}

func TestAnIssueSubjectMustBeThatIssueAndNotAPullRequest(t *testing.T) {
	h := newHarness(t)
	source := model.GitHubSource{Repository: baseRepositoryName, Subject: model.IssueSubject{Number: 7}}
	h.fake.Respond("/repos/owner/repo/issues/7", http.StatusOK, nil, `{"number": 7, "pull_request": {}}`)
	_, err := h.provider.Authorize(t.Context(), source)
	fixture.AsError[*client.SourceChangedError](t, err)
	h.fake.Respond("/repos/owner/repo/issues/7", http.StatusNotFound, nil, "")
	_, err = h.provider.Authorize(t.Context(), source)
	fixture.AsError[*client.AccessError](t, err)
}

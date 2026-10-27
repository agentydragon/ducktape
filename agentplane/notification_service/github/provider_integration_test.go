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
	asError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, h.next(claim)))
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
	retryAt := asError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, due)).Until
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
	requests := h.fake.total()
	h.ingest("issue_comment", comment(7))
	waiting := asError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, h.next(claim)))
	if !waiting.Until.Equal(retryAt) || h.fake.total() != requests {
		t.Fatalf("deferred until %v after %d new requests, want %v after none", waiting.Until, h.fake.total()-requests, retryAt)
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
	h.fake.forget()
	claim := h.claim()
	h.reconcileAll(claim)
	for path, want := range map[string]int{
		"/repos/owner/repo/installation": 1,
		"/repositories/100":              1,
		"/repos/owner/repo/pulls/7":      1,
		"/repos/owner/repo/pulls/8":      1,
	} {
		if got := h.fake.count(path); got != want {
			t.Errorf("%d requests for %s, want %d", got, path, want)
		}
	}

	requests := h.fake.total()
	h.restart()
	h.ingest("issue_comment", comment(7))
	h.reconcileAll(claim)
	if h.fake.total() != requests {
		t.Errorf("%d requests after a restart, want none", h.fake.total()-requests)
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
	h.fake.respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
		`{"number":7,"state":"open","base":{"ref":"main","sha":"`+testBaseSHA+`"},"mergeable":null,"head":{"sha":"`+testSHA+`","repo":{"id":200,"full_name":"fork/repo"}}}`)
	h.ingest("issue_comment", comment(7))
	claim := h.claim()
	h.reconcileAll(claim)

	if entries := h.page(subscription.InboxID).Entries; len(entries) != 1 {
		t.Fatalf("%d entries with mergeability unknown, want the webhook delivered", len(entries))
	}
	subject, err := store.NewGitHubSubjectKey(baseRepository, store.PullRequestSubject{Number: 7})
	if err != nil {
		t.Fatal(err)
	}
	state, err := h.provider.state.Subject(t.Context(), subject)
	if err != nil {
		t.Fatal(err)
	}
	freshness := time.Duration(DefaultSettings().FreshnessSeconds) * time.Second
	if state.LastSuccessAt == nil || !state.RepairAt.Before(state.LastSuccessAt.Add(freshness)) {
		t.Fatalf("subject state = %+v, want a persisted repair before normal freshness expiry", state)
	}
}

func TestAnInitialConflictProducesASyntheticMergeabilityEntry(t *testing.T) {
	h := newHarness(t)
	source := pullRequestSource(7, store.EventFilter{Event: store.EventMergeabilityChanged})
	subscription := h.follow(source, "follow")
	h.fake.respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
		`{"number":7,"state":"open","base":{"ref":"main","sha":"`+testBaseSHA+`"},"mergeable":false,"head":{"sha":"`+testSHA+`","repo":{"id":200,"full_name":"fork/repo"}}}`)
	h.reconcileAll(h.claim())

	entries := h.page(subscription.InboxID).Entries
	if len(entries) != 1 {
		t.Fatalf("%d entries, want the initial conflict observation", len(entries))
	}
	event, ok := entries[0].Event.(store.GitHubMergeabilityEvent)
	if !ok || event.AppID != testAppID || event.RepositoryID != baseRepository || event.PullRequestNumber != 7 || event.Sequence <= 0 {
		t.Fatalf("event = %#v, want the pull request's synthetic mergeability identity", entries[0].Event)
	}
	var payload struct {
		Repository        string    `json:"repository"`
		PullRequestNumber int64     `json:"pullRequestNumber"`
		BaseRef           string    `json:"baseRef"`
		HeadSHA           string    `json:"headSha"`
		BaseSHA           string    `json:"baseSha"`
		PreviousMergeable *bool     `json:"previousMergeable"`
		Mergeable         bool      `json:"mergeable"`
		ObservedAt        time.Time `json:"observedAt"`
	}
	if err := json.Unmarshal(entries[0].Payload, &payload); err != nil {
		t.Fatal(err)
	}
	if payload.Repository != baseRepositoryName || payload.PullRequestNumber != 7 || payload.BaseRef != "main" ||
		payload.HeadSHA != testSHA || payload.BaseSHA != testBaseSHA || payload.PreviousMergeable != nil || payload.Mergeable || payload.ObservedAt.IsZero() {
		t.Errorf("payload = %+v, want the initial conflict state observed from GitHub", payload)
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
		float64(DefaultSettings().FreshnessSeconds))
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
		deferred := asError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, due))
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
			func(h *harness) { h.fake.respond("/repositories/100", http.StatusBadGateway, nil, "") }, store.FailureUnavailable,
		},
		"a request GitHub refuses": {
			func(h *harness) { h.fake.respond("/repositories/100", http.StatusUnprocessableEntity, nil, "") }, store.FailureProcessingError,
		},
		"a suspended installation": {
			func(h *harness) {
				h.fake.respond("/repos/owner/repo/installation", http.StatusOK, nil, `{"id": 11, "suspended_at": "2026-01-01T00:00:00Z"}`)
			}, store.FailureAccessDenied,
		},
		"access revoked": {
			func(h *harness) { h.world.update(func(w *world) { w.revoked = true }) }, store.FailureAccessDenied,
		},
		"a repository without its name": {
			func(h *harness) { h.fake.respond("/repositories/100", http.StatusOK, nil, `{"id": 100}`) }, store.FailureProcessingError,
		},
	} {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t)
			subscription := h.follow(pullRequestSource(7), "follow")
			scenario.fail(h)
			claim := h.claim()
			due := h.next(claim)

			before := time.Now()
			deferred := asError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, due))
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

			requests := h.fake.total()
			asError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, due))
			if h.fake.total() != requests {
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
			asError[*worker.ProviderDeferred](t, h.provider.Reconcile(t.Context(), claim, h.next(claim)))
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
	h.fake.respond("/repositories/100", http.StatusOK, nil, `{"id": 100, "full_name": "owner/renamed"}`)
	h.reconcile(h.claim())

	for _, path := range []string{"/repos/owner/renamed/installation", "/repos/owner/renamed/pulls/7"} {
		if h.fake.count(path) != 1 {
			t.Errorf("%d requests for %s, want 1", h.fake.count(path), path)
		}
	}
	if name := h.text(`SELECT full_name FROM github_repository WHERE repository_id = 100`); name != "owner/renamed" {
		t.Errorf("repository name = %q, want the one GitHub reports", name)
	}
	view := h.subscription(subscription.ID)
	if source := view.Source.(store.GitHubSource); source.Repository != baseRepositoryName {
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
	h.fake.forget()
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
	asError[*store.RefreshDeferredError](t, err)
	close(resume)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	requireEqual(t, fence.Key, baseAccess)
	if h.fake.count("/repositories/100") != 1 || h.fake.count("/repos/owner/repo/installation") != 1 {
		t.Errorf("%d reads of the repository and %d of its installation, want one each",
			h.fake.count("/repositories/100"), h.fake.count("/repos/owner/repo/installation"))
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
	h.fake.respond("/repos/fork/repo/installation", http.StatusNotFound, nil, "")
	claim := h.claim()
	h.reconcile(claim)
	if entries := h.page(subscription.InboxID).Entries; len(entries) != 0 {
		t.Fatalf("%d entries while the App is not installed on the fork, want none", len(entries))
	}

	// A fork that was not covered is found when the subject's refresh comes due.
	h.fake.restore("/repos/fork/repo/installation")
	h.exec(`UPDATE github_subject SET last_success_at = now() - interval '1 hour'`)
	h.ingest("issue_comment", comment(7))
	h.reconcileAll(claim)
	requireEqual(t, payloads(t, h.page(subscription.InboxID).Entries), []any{asJSON(t, inFork), asJSON(t, comment(7))})
	if grants := h.count(`SELECT count(*) FROM github_repository_access WHERE repository_id = $1`, forkRepository); grants != 1 {
		t.Errorf("%d grants on the fork, want 1", grants)
	}
}

func TestAWebhookRacingTheBootstrapKeepsBothRevisionAssociations(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7, store.EventFilter{Event: store.EventCheckRun}), "follow")
	h.ingest("check_run", check(nextSHA))
	h.ingest("check_run", check(testSHA))
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
	h.reconcile(claim)
	requireEqual(t, payloads(t, h.page(subscription.InboxID).Entries), []any{asJSON(t, check(nextSHA)), asJSON(t, check(testSHA))})

	// A delayed older observation cannot erase the newer one.
	requests := h.fake.total()
	h.ingest("pull_request", pullRequestPayload(testSHA))
	h.ingest("check_run", check(nextSHA))
	h.reconcileAll(claim)
	if h.fake.total() != requests {
		t.Errorf("%d requests, want none while the shared state is fresh", h.fake.total()-requests)
	}
	if cursor := h.page(subscription.InboxID).Inbox.LastCursor; cursor != 3 {
		t.Errorf("last cursor = %d, want 3", cursor)
	}
}

func TestBranchActivityAndAFixedCommit(t *testing.T) {
	for kind, want := range map[string]int{"branch": 3, "commit": 1} {
		t.Run(kind, func(t *testing.T) {
			h := newHarness(t)
			var subject store.GitHubSubject = store.BranchSubject{Name: "devel"}
			if kind == "commit" {
				subject = store.CommitSubject{SHA: testSHA}
			}
			subscription := h.follow(store.GitHubSource{Repository: baseRepositoryName, Subject: subject}, "follow")
			base := map[string]any{
				"installation": map[string]any{"id": baseInstallation},
				"repository":   repositoryPayload(baseRepository, baseRepositoryName),
			}
			h.ingest("push", merged(base, map[string]any{"ref": "refs/heads/devel", "after": testSHA}))
			h.world.update(func(w *world) { w.head = nextSHA })
			h.ingest("check_run", check(testSHA))
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
	source := pullRequestSource(7, store.EventFilter{Event: store.EventCheckRun, Actions: []string{"completed"}})
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
	settings := DefaultSettings()
	settings.AppID = testAppID + 1
	other := NewProvider(h.provider.client, h.store, settings)

	requests := h.fake.total()
	failure := asError[*worker.ProviderError](t, other.Reconcile(t.Context(), claim, due))
	if failure.Kind != store.FailureSourceChanged || h.fake.total() != requests {
		t.Errorf("failure = %+v after %d new requests, want a source change without asking GitHub", failure, h.fake.total()-requests)
	}
}

func TestAuthorizeReturnsTheAccessASourceIsReadUnder(t *testing.T) {
	h := newHarness(t)
	for name, subject := range map[string]store.GitHubSubject{
		"a pull request": store.PullRequestSubject{Number: 7},
		"an issue":       store.IssueSubject{Number: 8},
		"a branch":       store.BranchSubject{Name: "feature/x"},
		"a commit":       store.CommitSubject{SHA: testSHA},
	} {
		access, err := h.provider.Authorize(t.Context(), store.GitHubSource{Repository: baseRepositoryName, Subject: subject})
		if err != nil || access != baseAccess {
			t.Errorf("%s: Authorize = %+v, %v; want %+v", name, access, err, baseAccess)
		}
	}
	// A branch that does not exist yet can be followed.
	h.world.update(func(w *world) { w.head = "" })
	source := store.GitHubSource{Repository: baseRepositoryName, Subject: store.BranchSubject{Name: "later"}}
	if access, err := h.provider.Authorize(t.Context(), source); err != nil || access != baseAccess {
		t.Errorf("a branch that does not exist: Authorize = %+v, %v", access, err)
	}
}

func TestAuthorizeFailsWithTheRateLimitAndItsDelay(t *testing.T) {
	h := newHarness(t)
	h.world.update(func(w *world) { w.limited = true })
	_, err := h.provider.Authorize(t.Context(), pullRequestSource(7))
	if limited := asError[*RateLimitedError](t, err); limited.RetrySeconds != 120 {
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
			h.fake.respond(failure.path, failure.status, nil, "")
			_, err := h.provider.Authorize(t.Context(), pullRequestSource(7))
			if denied := asError[*AccessError](t, err); denied.StatusCode != failure.status {
				t.Errorf("access error = %+v, want status %d", denied, failure.status)
			}
			var notInstalled *NotInstalledError
			if errors.As(err, &notInstalled) {
				t.Error("an access failure was taken for an uninstalled fork")
			}
		})
	}
}

func TestAnUninstalledForkIsOptionalButASuspendedOneIsAnError(t *testing.T) {
	h := newHarness(t)
	h.world.update(func(w *world) { w.fork = true })
	h.fake.respond("/repos/fork/repo/installation", http.StatusNotFound, nil, "")
	if access, err := h.provider.Authorize(t.Context(), pullRequestSource(7)); err != nil || access != baseAccess {
		t.Fatalf("Authorize with the fork uninstalled = %+v, %v", access, err)
	}
	h.fake.respond("/repos/fork/repo/installation", http.StatusOK, nil, `{"id": 22, "suspended_at": "2026-01-01T00:00:00Z"}`)
	_, err := h.provider.Authorize(t.Context(), pullRequestSource(7))
	asError[*SuspendedError](t, err)
}

func TestAnIssueSubjectMustBeThatIssueAndNotAPullRequest(t *testing.T) {
	h := newHarness(t)
	source := store.GitHubSource{Repository: baseRepositoryName, Subject: store.IssueSubject{Number: 7}}
	h.fake.respond("/repos/owner/repo/issues/7", http.StatusOK, nil, `{"number": 7, "pull_request": {}}`)
	_, err := h.provider.Authorize(t.Context(), source)
	asError[*SourceChangedError](t, err)
	h.fake.respond("/repos/owner/repo/issues/7", http.StatusNotFound, nil, "")
	_, err = h.provider.Authorize(t.Context(), source)
	asError[*AccessError](t, err)
}

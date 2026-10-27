package store

import (
	"errors"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/lib/pq"
)

func requireDeferred(t *testing.T, err error) *RefreshDeferredError {
	t.Helper()
	var deferred *RefreshDeferredError
	if !errors.As(err, &deferred) {
		t.Fatalf("error = %v, want *RefreshDeferredError", err)
	}
	return deferred
}

func (g githubFixture) status(t *testing.T, sub githubSubscription) GitHubStatus {
	t.Helper()
	status := g.get(t, sub.ID).GitHub
	if status == nil {
		t.Fatal("a GitHub subscription has no GitHub status")
	}
	return *status
}

func TestRefreshOfASharedRowIsSingleFlight(t *testing.T) {
	for name, key := range map[string]func(githubFixture, githubSubscription) GitHubRefreshKey{
		"access":  func(g githubFixture, _ githubSubscription) GitHubRefreshKey { return g.access() },
		"subject": func(_ githubFixture, sub githubSubscription) GitHubRefreshKey { return sub.Subject },
	} {
		t.Run(name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
			row := key(g, sub)

			var wg sync.WaitGroup
			leases := make([]*GitHubRefreshLease, 8)
			errs := make([]error, len(leases))
			for i := range leases {
				wg.Add(1)
				go func() {
					defer wg.Done()
					leases[i], errs[i] = g.state.Acquire(t.Context(), row)
				}()
			}
			wg.Wait()

			var held int
			for i, lease := range leases {
				if lease != nil {
					held++
					continue
				}
				requireDeferred(t, errs[i])
			}
			if held != 1 {
				t.Errorf("%d leases granted at once, want 1", held)
			}
		})
	}
}

func TestExpiredLeaseIsTakenOverAndTheOldHolderRecordsNothing(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	key := g.access()

	old, err := g.state.Acquire(t.Context(), key)
	if err != nil || old == nil {
		t.Fatalf("lease = %v, %v", old, err)
	}
	claimUntil := loadRefreshRow(t, g, key).claimUntil.Time
	if deferred := requireDeferredAcquire(t, g, key); !deferred.Until.Equal(claimUntil) {
		t.Errorf("deferred until %v, want the lease's end, %v", deferred.Until, claimUntil)
	}
	g.exec(t, `UPDATE github_repository_access SET claim_until = now() - interval '1 second' WHERE app_id = $1`, g.app)

	lease, err := g.state.Acquire(t.Context(), key)
	if err != nil || lease == nil {
		t.Fatalf("lease after the first expired = %v, %v", lease, err)
	}
	requireDeferred(t, g.state.Succeed(t.Context(), *old, GitHubSuccess{}))
	_, err = g.state.Fail(t.Context(), *old, FailureUnavailable, "stale failure", time.Minute)
	requireDeferred(t, err)
	if status := g.status(t, sub); status.Access[0].Failure != nil || status.Access[0].CurrentlyValid {
		t.Errorf("the replaced holder changed the grant: %+v", status.Access[0])
	}

	// A failure is shared: its backoff holds every worker off, and it is one episode for as long as its kind lasts.
	until, err := g.state.Fail(t.Context(), *lease, FailureRateLimited, "limited", 2*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	initial := g.status(t, sub).Access[0]
	if initial.RetryAt == nil || !initial.RetryAt.Equal(until) || initial.CurrentlyValid || initial.Failure == nil {
		t.Errorf("status after the failure = %+v, want a retry at %v and no valid grant", initial, until)
	}
	if deferred := requireDeferredAcquire(t, g, key); !deferred.Until.Equal(until) {
		t.Errorf("deferred until %v, want the backoff's end, %v", deferred.Until, until)
	}

	g.exec(t, `UPDATE github_repository_access SET next_attempt = now() WHERE app_id = $1`, g.app)
	lease, err = g.state.Acquire(t.Context(), key)
	if err != nil || lease == nil {
		t.Fatalf("lease after the backoff = %v, %v", lease, err)
	}
	if _, err := g.state.Fail(t.Context(), *lease, FailureRateLimited, "still limited", 2*time.Minute); err != nil {
		t.Fatal(err)
	}
	again := g.status(t, sub).Access[0]
	if !again.Failure.Since.Equal(initial.Failure.Since) || again.Failure.ObservedAt.Before(initial.Failure.ObservedAt) {
		t.Errorf("failure %+v after %+v, want the same episode, observed again", again.Failure, initial.Failure)
	}
	g.exec(t, `UPDATE github_repository_access SET next_attempt = now() WHERE app_id = $1`, g.app)
	lease, err = g.state.Acquire(t.Context(), key)
	if err != nil || lease == nil {
		t.Fatalf("lease after the second backoff = %v, %v", lease, err)
	}
	if _, err := g.state.Fail(t.Context(), *lease, FailureUnavailable, "now unavailable", 2*time.Minute); err != nil {
		t.Fatal(err)
	}
	if other := g.status(t, sub).Access[0]; other.Failure.Kind != FailureUnavailable || !other.Failure.Since.After(initial.Failure.Since) {
		t.Errorf("failure %+v, want a new episode of another kind", other.Failure)
	}

	g.exec(t, `UPDATE github_repository_access SET next_attempt = now() WHERE app_id = $1`, g.app)
	lease, err = g.state.Acquire(t.Context(), key)
	if err != nil || lease == nil {
		t.Fatalf("lease after the third backoff = %v, %v", lease, err)
	}
	if err := g.state.Succeed(t.Context(), *lease, GitHubSuccess{}); err != nil {
		t.Fatal(err)
	}
	healed := g.status(t, sub).Access[0]
	if !healed.CurrentlyValid || healed.Failure != nil || healed.RetryAt != nil || healed.RefreshingUntil != nil {
		t.Errorf("status after success = %+v, want a valid grant without failure", healed)
	}
	if subscription := g.get(t, sub.ID); subscription.Failure != nil || subscription.LastSuccessAt != nil {
		t.Errorf("subscription %+v took on the shared grant's state", subscription)
	}
	if entries := g.read(t, g.inbox, 0).Entries; len(entries) != 0 {
		t.Errorf("%d entries from refreshing", len(entries))
	}
}

func loadRefreshRow(t *testing.T, g githubFixture, key GitHubRefreshKey) refreshRow {
	t.Helper()
	row, err := loadRefresh(t.Context(), g.db, key)
	if err != nil {
		t.Fatal(err)
	}
	return row
}

func requireDeferredAcquire(t *testing.T, g githubFixture, key GitHubRefreshKey) *RefreshDeferredError {
	t.Helper()
	lease, err := g.state.Acquire(t.Context(), key)
	if lease != nil {
		t.Fatalf("got lease %+v, want it deferred", lease)
	}
	return requireDeferred(t, err)
}

func TestFreshRowsAreNotLeasedUntilTheyAreStale(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	base := g.validate(t, g.access())
	g.refreshSubject(t, sub.Subject, base)

	for name, key := range map[string]GitHubRefreshKey{"access": g.access(), "subject": sub.Subject} {
		if lease, err := g.state.Acquire(t.Context(), key); err != nil || lease != nil {
			t.Errorf("%s: lease = %v, %v, want none while fresh", name, lease, err)
		}
	}
	g.exec(t, `UPDATE github_repository_access SET valid_until = now() - interval '1 second' WHERE app_id = $1`, g.app)
	g.exec(t, `UPDATE github_subject SET last_success_at = now() - $2::interval WHERE repository_id = $1`, g.repository, "11 minutes")
	for name, key := range map[string]GitHubRefreshKey{"access": g.access(), "subject": sub.Subject} {
		if lease, err := g.state.Acquire(t.Context(), key); err != nil || lease == nil {
			t.Errorf("%s: lease = %v, %v, want one once stale", name, lease, err)
		}
	}
}

func TestSubjectRefreshIsNotRecordedUnderAGrantThatMovedOn(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	base := g.validate(t, g.access())
	lease, err := g.state.Acquire(t.Context(), sub.Subject)
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}

	g.deliver(t, "installation", withAction("suspend"))

	// The subject's lease died with the installation's event, and the fence it read under is no longer good.
	requireDeferred(t, g.state.Succeed(t.Context(), *lease, GitHubSuccess{Fences: []GitHubAccessFence{base}}))
	lease, err = g.state.Acquire(t.Context(), sub.Subject)
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}
	requireDeferred(t, g.state.Succeed(t.Context(), *lease, GitHubSuccess{
		Fences:    []GitHubAccessFence{base},
		Revisions: []GitHubHeadRevision{{Repository: GitHubRepository{ID: g.repository, Name: testRepositoryName}, SHA: commitA}},
	}))
	if got := g.count(t, `SELECT count(*) FROM github_subject_revision WHERE repository_id = $1`, g.repository); got != 0 {
		t.Errorf("%d revisions recorded under a stale fence, want none", got)
	}
}

func TestSuccessfulAccessRefreshRenamesTheRepository(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	lease, err := g.state.Acquire(t.Context(), g.access())
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}
	renamed := "test-owner/renamed"
	if err := g.state.Succeed(t.Context(), *lease, GitHubSuccess{RepositoryName: &renamed}); err != nil {
		t.Fatal(err)
	}

	if name := g.repositoryName(t, g.repository); name != renamed {
		t.Errorf("repository named %q, want %q", name, renamed)
	}
	// The repository subscribed to is the numeric one: the name it was asked for is what was stored.
	if source := g.get(t, sub.ID).Source.(GitHubSource); source.Repository != testRepositoryName {
		t.Errorf("subscription follows %q, want the name it was created with", source.Repository)
	}
	if !g.status(t, sub).Access[0].CurrentlyValid {
		t.Error("the grant is not valid after the refresh")
	}
}

func TestSharedFailureFieldsAreConstrained(t *testing.T) {
	g := newGitHubFixture(t)
	g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	for _, row := range []struct {
		table, where string
		arg          int64
	}{
		{"github_repository_access", "app_id = $1", g.app},
		{"github_subject", "repository_id = $1", g.repository},
	} {
		t.Run(row.table, func(t *testing.T) {
			for name, set := range map[string]string{
				"a failure without its kind and times": "error = 'partial observation'",
				"a kind that is not one":               `error = 'bad kind', error_kind = 'healthy', error_since = now(), error_observed_at = now()`,
			} {
				_, err := g.db.ExecContext(t.Context(), "UPDATE "+row.table+" SET "+set+" WHERE "+row.where, row.arg)
				var violation *pq.Error
				if !errors.As(err, &violation) || violation.Code != "23514" {
					t.Errorf("%s: error = %v, want a check violation", name, err)
				}
			}
		})
	}
}

func TestSharedFailuresAreProjectedWithoutBeingCopiedIntoSubscriptions(t *testing.T) {
	g := newGitHubFixture(t)
	source := g.source(PullRequestSubject{Number: 7})
	first, second := g.follow(t, source, "first"), g.follow(t, source, "second")
	lease, err := g.state.Acquire(t.Context(), g.access())
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}
	if _, err := g.state.Fail(t.Context(), *lease, FailureRateLimited, "limited", time.Minute); err != nil {
		t.Fatal(err)
	}

	listed, err := g.ListSubscriptions(t.Context(), g.incarnation(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(listed) != 2 || listed[0].GitHub == nil || listed[1].GitHub == nil {
		t.Fatalf("listed %+v, want both subscriptions with a GitHub status", listed)
	}
	requireEqual(t, listed[0].GitHub, listed[1].GitHub)
	if kind := listed[0].GitHub.Access[0].Failure.Kind; kind != FailureRateLimited {
		t.Errorf("failure kind = %q, want %q", kind, FailureRateLimited)
	}
	for _, subscription := range listed {
		if subscription.Failure != nil || subscription.LastSuccessAt != nil || subscription.RetryAt != nil {
			t.Errorf("subscription %s took on the shared failure: %+v", subscription.ID, subscription)
		}
	}
	for _, sub := range []githubSubscription{first, second} {
		if state := g.subscriptionState(t, sub.ID); state.errorKind.Valid {
			t.Errorf("subscription row %s records error kind %q", sub.ID, state.errorKind.String)
		}
	}
	if got := g.count(t, `SELECT count(*) FROM github_repository_access WHERE app_id = $1`, g.app); got != 1 {
		t.Errorf("%d access rows, want the one both share", got)
	}
	if page := g.read(t, g.inbox, 0); page.Inbox.LastCursor != 0 || len(page.Entries) != 0 || page.Notice != nil {
		t.Errorf("page %+v, want an untouched inbox", page)
	}
}

func TestStatusNamesTheGrantsAHeadInAForkAdds(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	status := g.status(t, sub)
	if len(status.Access) != 1 || status.Access[0].Key != g.access() || status.Subject.Key != sub.Subject {
		t.Fatalf("status = %+v, want the repository's grant and the subject", status)
	}
	if status.Access[0].CurrentlyValid || status.Access[0].LastSuccessAt != nil || status.Access[0].CheckedAt != nil || status.Access[0].ValidUntil != nil {
		t.Errorf("a grant never checked reads as %+v", status.Access[0])
	}

	forkRepository := g.fork(1000)
	fork := g.grant(t, g.installation+1, forkRepository)
	// A grant on a repository that holds no revision of the subject is not the subject's.
	g.grant(t, g.installation+2, g.fork(2000))
	g.addRevisions(t, sub.Subject, GitHubHeadRevision{Repository: forkRepository, SHA: commitA})
	g.validate(t, g.access())
	forkLease, err := g.state.Acquire(t.Context(), fork)
	if err != nil || forkLease == nil {
		t.Fatalf("lease = %v, %v", forkLease, err)
	}

	status = g.status(t, sub)
	var keys []GitHubAccessKey
	for _, access := range status.Access {
		keys = append(keys, access.Key)
	}
	if want := []GitHubAccessKey{g.access(), fork}; !slices.Equal(keys, want) {
		t.Fatalf("grants = %+v, want %+v in repository order", keys, want)
	}
	base, forked := status.Access[0], status.Access[1]
	if !base.CurrentlyValid || base.CheckedAt == nil || base.ValidUntil == nil || base.LastSuccessAt == nil {
		t.Errorf("validated grant reads as %+v", base)
	}
	if forked.CurrentlyValid || forked.RefreshingUntil == nil {
		t.Errorf("grant being refreshed reads as %+v, want it leased and not valid", forked)
	}

	// A subject's failure is projected with when it will be tried again, and ends with the next success.
	subjectLease, err := g.state.Acquire(t.Context(), sub.Subject)
	if err != nil || subjectLease == nil {
		t.Fatalf("lease = %v, %v", subjectLease, err)
	}
	until, err := g.state.Fail(t.Context(), *subjectLease, FailureAccessDenied, "denied", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	failed := g.status(t, sub).Subject
	if failed.Failure == nil || failed.Failure.Kind != FailureAccessDenied || failed.RetryAt == nil || !failed.RetryAt.Equal(until) {
		t.Errorf("failed subject reads as %+v, want a retry at %v", failed, until)
	}
	g.exec(t, `UPDATE github_subject SET next_attempt = now() WHERE repository_id = $1`, g.repository)
	g.refreshSubject(t, sub.Subject, g.validate(t, g.access()))
	if healed := g.status(t, sub).Subject; healed.Failure != nil || healed.RetryAt != nil || healed.LastSuccessAt == nil {
		t.Errorf("recovered subject reads as %+v", healed)
	}
}

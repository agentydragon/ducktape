package state

import (
	"errors"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/lib/pq"
)

func TestRefreshOfASharedRowIsSingleFlight(t *testing.T) {
	for name, key := range map[string]func(fixture, SubjectKey) RefreshKey{
		"access":  func(g fixture, _ SubjectKey) RefreshKey { return g.access() },
		"subject": func(_ fixture, subject SubjectKey) RefreshKey { return subject },
	} {
		t.Run(name, func(t *testing.T) {
			g := newFixture(t)
			subject := g.subject(t, 7)
			row := key(g, subject)

			var wg sync.WaitGroup
			leases := make([]*RefreshLease, 8)
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
	g := newFixture(t)
	subject := g.subject(t, 7)
	key := g.access()

	old, err := g.state.Acquire(t.Context(), key)
	if err != nil || old == nil {
		t.Fatalf("lease = %v, %v", old, err)
	}
	claimUntil := *g.status(t, subject).Access[0].RefreshingUntil
	if deferred := requireDeferredAcquire(t, g, key); !deferred.Until.Equal(claimUntil) {
		t.Errorf("deferred until %v, want the lease's end, %v", deferred.Until, claimUntil)
	}
	g.exec(t, `UPDATE github_repository_access SET claim_until = now() - interval '1 second' WHERE app_id = $1`, g.app)

	lease, err := g.state.Acquire(t.Context(), key)
	if err != nil || lease == nil {
		t.Fatalf("lease after the first expired = %v, %v", lease, err)
	}
	requireDeferred(t, g.state.CompleteAccess(t.Context(), *old, nil))
	_, err = g.state.Fail(t.Context(), *old, model.FailureUnavailable, "stale failure", time.Minute)
	requireDeferred(t, err)
	if status := g.status(t, subject); status.Access[0].Failure != nil || status.Access[0].CurrentlyValid {
		t.Errorf("the replaced holder changed the grant: %+v", status.Access[0])
	}

	// A failure is shared: its backoff holds every worker off, and it is one episode for as long as its kind lasts.
	until, err := g.state.Fail(t.Context(), *lease, model.FailureRateLimited, "limited", 2*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	initial := g.status(t, subject).Access[0]
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
	if _, err := g.state.Fail(t.Context(), *lease, model.FailureRateLimited, "still limited", 2*time.Minute); err != nil {
		t.Fatal(err)
	}
	again := g.status(t, subject).Access[0]
	if !again.Failure.Since.Equal(initial.Failure.Since) || again.Failure.ObservedAt.Before(initial.Failure.ObservedAt) {
		t.Errorf("failure %+v after %+v, want the same episode, observed again", again.Failure, initial.Failure)
	}
	g.exec(t, `UPDATE github_repository_access SET next_attempt = now() WHERE app_id = $1`, g.app)
	lease, err = g.state.Acquire(t.Context(), key)
	if err != nil || lease == nil {
		t.Fatalf("lease after the second backoff = %v, %v", lease, err)
	}
	if _, err := g.state.Fail(t.Context(), *lease, model.FailureUnavailable, "now unavailable", 2*time.Minute); err != nil {
		t.Fatal(err)
	}
	if other := g.status(t, subject).Access[0]; other.Failure.Kind != model.FailureUnavailable || !other.Failure.Since.After(initial.Failure.Since) {
		t.Errorf("failure %+v, want a new episode of another kind", other.Failure)
	}

	g.exec(t, `UPDATE github_repository_access SET next_attempt = now() WHERE app_id = $1`, g.app)
	lease, err = g.state.Acquire(t.Context(), key)
	if err != nil || lease == nil {
		t.Fatalf("lease after the third backoff = %v, %v", lease, err)
	}
	if err := g.state.CompleteAccess(t.Context(), *lease, nil); err != nil {
		t.Fatal(err)
	}
	healed := g.status(t, subject).Access[0]
	if !healed.CurrentlyValid || healed.Failure != nil || healed.RetryAt != nil || healed.RefreshingUntil != nil {
		t.Errorf("status after success = %+v, want a valid grant without failure", healed)
	}
}

func TestFreshRowsAreNotLeasedUntilTheyAreStale(t *testing.T) {
	g := newFixture(t)
	subject := g.subject(t, 7)
	base := g.validate(t, g.access())
	g.refreshSubject(t, subject, base)

	for name, key := range map[string]RefreshKey{"access": g.access(), "subject": subject} {
		if lease, err := g.state.Acquire(t.Context(), key); err != nil || lease != nil {
			t.Errorf("%s: lease = %v, %v, want none while fresh", name, lease, err)
		}
	}
	g.exec(t, `UPDATE github_repository_access SET valid_until = now() - interval '1 second' WHERE app_id = $1`, g.app)
	g.exec(t, `UPDATE github_subject SET last_success_at = now() - $2::interval WHERE repository_id = $1`, g.repository, "11 minutes")
	for name, key := range map[string]RefreshKey{"access": g.access(), "subject": subject} {
		if lease, err := g.state.Acquire(t.Context(), key); err != nil || lease == nil {
			t.Errorf("%s: lease = %v, %v, want one once stale", name, lease, err)
		}
	}
}

func TestSubjectRefreshIsNotRecordedUnderAGrantThatMovedOn(t *testing.T) {
	g := newFixture(t)
	subject := g.subject(t, 7)
	base := g.validate(t, g.access())
	lease, err := g.state.Acquire(t.Context(), subject)
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}

	g.invalidateInstallation(t)

	// The subject's lease died with the installation's event, and the fence it read under is no longer good.
	requireDeferred(t, g.state.CompleteSubject(t.Context(), *lease, []AccessFence{base}, SubjectObservation{}))
	lease, err = g.state.Acquire(t.Context(), subject)
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}
	requireDeferred(t, g.state.CompleteSubject(t.Context(), *lease, []AccessFence{base}, SubjectObservation{Revisions: []model.GitHubHeadRevision{{Repository: model.GitHubRepository{ID: g.repository, Name: testRepositoryName}, SHA: commitA}}}))
	if got := g.count(t, `SELECT count(*) FROM github_subject_revision WHERE repository_id = $1`, g.repository); got != 0 {
		t.Errorf("%d revisions recorded under a stale fence, want none", got)
	}
}

func TestSuccessfulAccessRefreshRenamesTheRepository(t *testing.T) {
	g := newFixture(t)
	subject := g.subject(t, 7)
	lease, err := g.state.Acquire(t.Context(), g.access())
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}
	renamed := "test-owner/renamed"
	if err := g.state.CompleteAccess(t.Context(), *lease, &renamed); err != nil {
		t.Fatal(err)
	}

	if name, err := g.state.RepositoryName(t.Context(), g.repository); err != nil || name != renamed {
		t.Errorf("repository name = %q, %v, want %q", name, err, renamed)
	}
	if !g.status(t, subject).Access[0].CurrentlyValid {
		t.Error("the grant is not valid after the refresh")
	}
}

func TestSharedFailureFieldsAreConstrained(t *testing.T) {
	g := newFixture(t)
	g.subject(t, 7)
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

func TestStatusNamesTheGrantsAHeadInAForkAdds(t *testing.T) {
	g := newFixture(t)
	subject := g.subject(t, 7)
	status := g.status(t, subject)
	if len(status.Access) != 1 || status.Access[0].Key != g.access() || status.Subject.Key != subject {
		t.Fatalf("status = %+v, want the repository's grant and the subject", status)
	}
	if status.Access[0].CurrentlyValid || status.Access[0].LastSuccessAt != nil || status.Access[0].CheckedAt != nil || status.Access[0].ValidUntil != nil {
		t.Errorf("a grant never checked reads as %+v", status.Access[0])
	}

	forkRepository := g.fork(1000)
	fork := g.grant(t, g.installation+1, forkRepository)
	// A grant on a repository that holds no revision of the subject is not the subject's.
	g.grant(t, g.installation+2, g.fork(2000))
	g.addRevisions(t, subject, model.GitHubHeadRevision{Repository: forkRepository, SHA: commitA})
	g.validate(t, g.access())
	forkLease, err := g.state.Acquire(t.Context(), fork)
	if err != nil || forkLease == nil {
		t.Fatalf("lease = %v, %v", forkLease, err)
	}

	status = g.status(t, subject)
	var keys []AccessKey
	for _, access := range status.Access {
		keys = append(keys, access.Key)
	}
	if want := []AccessKey{g.access(), fork}; !slices.Equal(keys, want) {
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
	subjectLease, err := g.state.Acquire(t.Context(), subject)
	if err != nil || subjectLease == nil {
		t.Fatalf("lease = %v, %v", subjectLease, err)
	}
	until, err := g.state.Fail(t.Context(), *subjectLease, model.FailureAccessDenied, "denied", time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	failed := g.status(t, subject).Subject
	if failed.Failure == nil || failed.Failure.Kind != model.FailureAccessDenied || failed.RetryAt == nil || !failed.RetryAt.Equal(until) {
		t.Errorf("failed subject reads as %+v, want a retry at %v", failed, until)
	}
	g.exec(t, `UPDATE github_subject SET next_attempt = now() WHERE repository_id = $1`, g.repository)
	g.refreshSubject(t, subject, g.validate(t, g.access()))
	if healed := g.status(t, subject).Subject; healed.Failure != nil || healed.RetryAt != nil || healed.LastSuccessAt == nil {
		t.Errorf("recovered subject reads as %+v", healed)
	}
}

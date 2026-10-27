package store

import (
	"database/sql"
	"errors"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

type storedPullRequestObservation struct {
	baseRef       string
	headSHA       string
	baseSHA       string
	open          bool
	pending       bool
	mergeable     sql.NullBool
	eventSequence int64
}

func (g githubFixture) pullRequestObservation(t *testing.T, number int64) storedPullRequestObservation {
	t.Helper()
	var observation storedPullRequestObservation
	if err := g.db.QueryRowContext(t.Context(), `SELECT base_ref, head_sha, base_sha, open, pending, mergeable, event_sequence
		FROM github_pull_request WHERE repository_id = $1 AND number = $2`, g.repository, number).Scan(
		&observation.baseRef, &observation.headSHA, &observation.baseSHA, &observation.open, &observation.pending,
		&observation.mergeable, &observation.eventSequence,
	); err != nil {
		t.Fatal(err)
	}
	return observation
}

func (g githubFixture) mergeabilitySequences(t *testing.T, number int64) []int64 {
	t.Helper()
	rows, err := g.db.QueryContext(t.Context(), `SELECT sequence FROM github_mergeability_event
		WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3 AND pull_request_number = $4
		ORDER BY sequence`, g.app, g.installation, g.repository, number)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var sequences []int64
	for rows.Next() {
		var sequence int64
		if err := rows.Scan(&sequence); err != nil {
			t.Fatal(err)
		}
		sequences = append(sequences, sequence)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return sequences
}

func (g githubFixture) forcePullRequestRefreshDue(t *testing.T, sub githubSubscription) {
	t.Helper()
	g.exec(t, `UPDATE github_subject SET last_success_at = now() - interval '11 minutes', next_attempt = now(),
		claim = NULL, claim_until = NULL WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`,
		sub.Subject.RepositoryID, sub.Subject.Kind, sub.Subject.Key)
}

func (g githubFixture) observePullRequest(t *testing.T, sub githubSubscription, observation GitHubPullRequestObservation) {
	t.Helper()
	g.forcePullRequestRefreshDue(t, sub)
	lease, err := g.state.Acquire(t.Context(), sub.Subject)
	if err != nil || lease == nil {
		t.Fatalf("pull request refresh lease = %v, %v", lease, err)
	}
	base := g.validate(t, g.access())
	if err := g.state.Succeed(t.Context(), *lease, GitHubSuccess{
		Fences: []GitHubAccessFence{base}, PullRequest: &observation,
	}); err != nil {
		t.Fatal(err)
	}
}

func mergeabilityEntries(t *testing.T, g githubFixture) []Entry {
	t.Helper()
	var entries []Entry
	for _, entry := range g.read(t, g.inbox, 0).Entries {
		if _, ok := entry.Event.(GitHubMergeabilityEvent); ok {
			entries = append(entries, entry)
		}
	}
	return entries
}

func sequencesForSubscription(t *testing.T, entries []Entry, subscription uuid.UUID) []int64 {
	t.Helper()
	var sequences []int64
	for _, entry := range entries {
		if !slices.Contains(entry.SubscriptionIDs, subscription) {
			continue
		}
		event, ok := entry.Event.(GitHubMergeabilityEvent)
		if !ok {
			t.Fatalf("entry event = %#v, want GitHubMergeabilityEvent", entry.Event)
		}
		sequences = append(sequences, event.Sequence)
	}
	return sequences
}

func TestPendingMergeabilityRetrySurvivesStateReconstructionAndKeepsLastResult(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	falseValue := false
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: true, Mergeable: &falseValue,
	})
	firstSequence := g.mergeabilitySequences(t, 7)
	if len(firstSequence) != 1 {
		t.Fatalf("initial conflict events = %v, want one", firstSequence)
	}

	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, Open: true, Mergeable: nil,
	})
	stored := g.pullRequestObservation(t, 7)
	if stored.baseRef != "main" || stored.headSHA != commitC || stored.baseSHA != commitB || !stored.open || !stored.pending ||
		!stored.mergeable.Valid || stored.mergeable.Bool || stored.eventSequence != firstSequence[0] {
		t.Errorf("pending observation = %+v, want updated revisions, pending state, preserved false result and sequence", stored)
	}

	state, err := g.state.Subject(t.Context(), sub.Subject)
	if err != nil {
		t.Fatal(err)
	}
	g.state = NewGitHubState(g.Store, testFreshness)
	reconstructed, err := g.state.Subject(t.Context(), sub.Subject)
	if err != nil {
		t.Fatal(err)
	}
	if !state.RepairAt.After(time.Now()) || !reconstructed.RepairAt.Equal(state.RepairAt) {
		t.Fatalf("repair deadline before/after reconstruction = %v / %v, want the persisted pending deadline", state.RepairAt, reconstructed.RepairAt)
	}
	lease, err := g.state.Acquire(t.Context(), sub.Subject)
	if lease != nil || err != nil {
		t.Fatalf("refresh during pending window = %v, %v; want no lease and no error until %v", lease, err, reconstructed.RepairAt)
	}

	g.exec(t, `UPDATE github_subject SET next_attempt = now() - interval '1 second'
		WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`, sub.Subject.RepositoryID, sub.Subject.Kind, sub.Subject.Key)
	lease, err = g.state.Acquire(t.Context(), sub.Subject)
	if err != nil || lease == nil {
		t.Fatalf("refresh after pending deadline = %v, %v", lease, err)
	}
	if err := g.state.Succeed(t.Context(), *lease, GitHubSuccess{
		Fences: []GitHubAccessFence{g.validate(t, g.access())},
		PullRequest: &GitHubPullRequestObservation{
			BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, Open: true, Mergeable: &falseValue,
		},
	}); err != nil {
		t.Fatal(err)
	}
	if got := g.mergeabilitySequences(t, 7); !slices.Equal(got, firstSequence) {
		t.Errorf("false → pending → false event sequences = %v, want no duplicate after %v", got, firstSequence)
	}
}

func TestInitiallyMergeablePullRequestDoesNotEmitUntilItConflicts(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	trueValue := true
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: true, Mergeable: &trueValue,
	})
	if got := g.mergeabilitySequences(t, 7); len(got) != 0 {
		t.Fatalf("initial true observation sequences = %v, want none", got)
	}
	g.reconcileAll(t, sub)
	if entries := mergeabilityEntries(t, g); len(entries) != 0 {
		t.Fatalf("entries after initial true observation = %v, want none", entries)
	}

	falseValue := false
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: true, Mergeable: &falseValue,
	})
	sequences := g.mergeabilitySequences(t, 7)
	if len(sequences) != 1 {
		t.Fatalf("initial conflict after true sequences = %v, want one", sequences)
	}
	g.reconcileAll(t, sub)
	if got := sequencesForSubscription(t, mergeabilityEntries(t, g), sub.ID); !slices.Equal(got, sequences) {
		t.Errorf("delivered sequences = %v, want %v", got, sequences)
	}
}

func TestMergeabilityTransitionsUseDefaultFiltersAndDeduplicateSubscribers(t *testing.T) {
	g := newGitHubFixture(t)
	defaultOne := g.follow(t, g.source(PullRequestSubject{Number: 7}), "default-one")
	defaultTwo := g.follow(t, g.source(PullRequestSubject{Number: 7}), "default-two")
	explicitComments := g.follow(t, g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventIssueComment}), "comments")
	falseValue, trueValue := false, true
	observations := []GitHubPullRequestObservation{
		{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: true, Mergeable: &falseValue},
		{BaseRef: "main", HeadSHA: commitB, BaseSHA: commitB, Open: true, Mergeable: &trueValue},
		{BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, Open: true, Mergeable: &falseValue},
	}
	for _, observation := range observations {
		g.observePullRequest(t, defaultOne, observation)
		g.reconcileAll(t, defaultOne, defaultTwo, explicitComments)
	}

	sequences := g.mergeabilitySequences(t, 7)
	if len(sequences) != 3 || !(sequences[0] < sequences[1] && sequences[1] < sequences[2]) {
		t.Fatalf("false → true → false event sequences = %v, want three increasing transitions", sequences)
	}
	entries := mergeabilityEntries(t, g)
	if len(entries) != 3 {
		t.Fatalf("mergeability entries = %d, want one per transition: %+v", len(entries), entries)
	}
	for i, entry := range entries {
		event, ok := entry.Event.(GitHubMergeabilityEvent)
		if !ok || event.Sequence != sequences[i] || event.AppID != g.app || event.RepositoryID != g.repository || event.PullRequestNumber != 7 {
			t.Errorf("entry %d event = %#v, want transition sequence %d for this pull request", i, entry.Event, sequences[i])
		}
		if want := sortedIDs(defaultOne.ID, defaultTwo.ID); !slices.Equal(entry.SubscriptionIDs, want) {
			t.Errorf("entry %d subscriptions = %v, want both default subscribers %v", i, entry.SubscriptionIDs, want)
		}
	}
	if got := sequencesForSubscription(t, entries, explicitComments.ID); len(got) != 0 {
		t.Errorf("explicit comments filter received mergeability sequences %v", got)
	}
}

func TestNewSubscriberToCachedConflictGetsOnlyTheCurrentConflict(t *testing.T) {
	g := newGitHubFixture(t)
	seed := g.follow(t, g.source(PullRequestSubject{Number: 7}), "seed")
	falseValue, trueValue := false, true
	for _, observation := range []GitHubPullRequestObservation{
		{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: true, Mergeable: &falseValue},
		{BaseRef: "main", HeadSHA: commitB, BaseSHA: commitB, Open: true, Mergeable: &trueValue},
		{BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, Open: true, Mergeable: &falseValue},
	} {
		g.observePullRequest(t, seed, observation)
		g.reconcileAll(t, seed)
	}
	allSequences := g.mergeabilitySequences(t, 7)
	if len(allSequences) != 3 {
		t.Fatalf("seed transition sequences = %v, want three", allSequences)
	}

	newSubscriber := g.follow(t, g.source(PullRequestSubject{Number: 7}), "late-subscriber")
	g.reconcileAll(t, newSubscriber)
	entries := mergeabilityEntries(t, g)
	if got := sequencesForSubscription(t, entries, seed.ID); !slices.Equal(got, allSequences) {
		t.Errorf("seed received sequences %v, want %v", got, allSequences)
	}
	if got := sequencesForSubscription(t, entries, newSubscriber.ID); !slices.Equal(got, []int64{allSequences[2]}) {
		t.Errorf("new subscriber received sequences %v, want only current conflict %d", got, allSequences[2])
	}
}

func TestBaseBranchPushInvalidatesAndFencesAnInFlightPullRequestRefresh(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	falseValue := false
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: true, Mergeable: &falseValue,
	})
	before, err := g.state.Subject(t.Context(), sub.Subject)
	if err != nil {
		t.Fatal(err)
	}
	g.exec(t, `UPDATE github_subject SET last_success_at = now() - interval '11 minutes'
		WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`, sub.Subject.RepositoryID, sub.Subject.Kind, sub.Subject.Key)
	lease, err := g.state.Acquire(t.Context(), sub.Subject)
	if err != nil || lease == nil {
		t.Fatalf("in-flight refresh lease = %v, %v", lease, err)
	}
	g.exec(t, `UPDATE subscription SET next_attempt = now() + interval '1 hour' WHERE id = $1`, sub.ID)

	g.deliver(t, "push", onBranch("main"), atCommit(commitC), labelled("base branch moved"))
	after, err := g.state.Subject(t.Context(), sub.Subject)
	if err != nil {
		t.Fatal(err)
	}
	if after.Generation != before.Generation+1 || after.LastSuccessAt != nil {
		t.Errorf("subject after base push = %+v, want invalidated from %+v", after, before)
	}
	if next := g.subscriptionState(t, sub.ID).nextAttempt; !next.Valid || next.Time.After(time.Now()) {
		t.Errorf("subscription next attempt = %v, want it due after the base push", next)
	}
	err = g.state.Succeed(t.Context(), *lease, GitHubSuccess{
		Fences: []GitHubAccessFence{g.validate(t, g.access())},
		PullRequest: &GitHubPullRequestObservation{
			BaseRef: "main", HeadSHA: commitC, BaseSHA: commitC, Open: true, Mergeable: &falseValue,
		},
	})
	var deferred *RefreshDeferredError
	if !errors.As(err, &deferred) {
		t.Errorf("stale in-flight pull request result = %v, want it deferred", err)
	}
	stored := g.pullRequestObservation(t, 7)
	got := g.mergeabilitySequences(t, 7)
	if stored.headSHA != commitA || stored.baseSHA != commitB || len(got) != 1 {
		t.Errorf("in-flight result changed observation or events: stored=%+v sequences=%v", stored, got)
	}
}

func TestBaseBranchPushPreservesPullRequestRefreshFailureBackoff(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	falseValue := false
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: true, Mergeable: &falseValue,
	})
	g.forcePullRequestRefreshDue(t, sub)
	lease, err := g.state.Acquire(t.Context(), sub.Subject)
	if err != nil || lease == nil {
		t.Fatalf("failed refresh lease = %v, %v", lease, err)
	}
	retryAt, err := g.state.Fail(t.Context(), *lease, FailureRateLimited, "rate limited", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	before, err := g.state.Subject(t.Context(), sub.Subject)
	if err != nil {
		t.Fatal(err)
	}

	g.deliver(t, "push", onBranch("main"), atCommit(commitC), labelled("base branch moved"))
	after, err := g.state.Subject(t.Context(), sub.Subject)
	if err != nil {
		t.Fatal(err)
	}
	if after.Generation != before.Generation+1 || after.LastSuccessAt != nil {
		t.Errorf("subject after base push = %+v, want invalidated from %+v", after, before)
	}
	blocked, err := g.state.Acquire(t.Context(), sub.Subject)
	deferred := requireDeferred(t, err)
	if blocked != nil || !deferred.Until.Equal(retryAt) {
		t.Errorf("refresh during rate-limit backoff = %v, %v; want it held until %v", blocked, deferred.Until, retryAt)
	}
}

func TestUnrelatedBranchPushDoesNotInvalidatePullRequestRefresh(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	falseValue := false
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: true, Mergeable: &falseValue,
	})
	before, err := g.state.Subject(t.Context(), sub.Subject)
	if err != nil {
		t.Fatal(err)
	}

	g.deliver(t, "push", onBranch("release"), atCommit(commitC), labelled("unrelated branch moved"))
	after, err := g.state.Subject(t.Context(), sub.Subject)
	if err != nil {
		t.Fatal(err)
	}
	if after.Generation != before.Generation || after.LastSuccessAt == nil || before.LastSuccessAt == nil ||
		!after.LastSuccessAt.Equal(*before.LastSuccessAt) || !after.RepairAt.Equal(before.RepairAt) {
		t.Errorf("subject after unrelated push = %+v, want it unchanged from %+v", after, before)
	}
}

func TestClosedPullRequestsDoNotEmitMergeabilityEvents(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	falseValue := false
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: false, Mergeable: &falseValue,
	})
	if got := g.mergeabilitySequences(t, 7); len(got) != 0 {
		t.Fatalf("closed PR events = %v, want none", got)
	}
	g.reconcileAll(t, sub)
	if entries := mergeabilityEntries(t, g); len(entries) != 0 {
		t.Fatalf("closed PR entries = %v, want none", entries)
	}

	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: true, Mergeable: &falseValue,
	})
	openSequences := g.mergeabilitySequences(t, 7)
	if len(openSequences) != 1 {
		t.Fatalf("open conflict events = %v, want one", openSequences)
	}
	g.reconcileAll(t, sub)
	trueValue := true
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, Open: false, Mergeable: &trueValue,
	})
	if got := g.mergeabilitySequences(t, 7); !slices.Equal(got, openSequences) {
		t.Errorf("event sequences after closing the PR = %v, want %v", got, openSequences)
	}
	if entries := mergeabilityEntries(t, g); len(entries) != 1 {
		t.Errorf("entries after closing the PR = %v, want only the open conflict", entries)
	}
}

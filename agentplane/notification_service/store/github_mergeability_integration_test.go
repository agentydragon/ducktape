package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

type storedPullRequestObservation struct {
	baseRef                 string
	headSHA                 string
	baseSHA                 string
	state                   GitHubPullRequestState
	mergeability            GitHubMergeableState
	lastSettledMergeability GitHubMergeableState
	eventSequence           int64
}

func (g githubFixture) pullRequestObservation(t *testing.T, number int64) storedPullRequestObservation {
	t.Helper()
	var observation storedPullRequestObservation
	if err := g.db.QueryRowContext(t.Context(), `SELECT base_ref, head_sha, base_sha, state, mergeability,
		last_settled_mergeability, event_sequence
		FROM github_pull_request WHERE repository_id = $1 AND number = $2`, g.repository, number).Scan(
		&observation.baseRef, &observation.headSHA, &observation.baseSHA, &observation.state, &observation.mergeability,
		&observation.lastSettledMergeability, &observation.eventSequence,
	); err != nil {
		t.Fatal(err)
	}
	return observation
}

func (g githubFixture) pullRequestRetryAt(t *testing.T, sub githubSubscription) sql.NullTime {
	t.Helper()
	var nextAttempt sql.NullTime
	if err := g.db.QueryRowContext(t.Context(), `SELECT next_attempt FROM github_subject
		WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`,
		sub.Subject.RepositoryID, sub.Subject.Kind, sub.Subject.Key).Scan(&nextAttempt); err != nil {
		t.Fatal(err)
	}
	return nextAttempt
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
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
	})
	firstSequence := g.mergeabilitySequences(t, 7)
	if len(firstSequence) != 1 {
		t.Fatalf("initial conflict events = %v, want one", firstSequence)
	}

	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})
	stored := g.pullRequestObservation(t, 7)
	if stored.baseRef != "main" || stored.headSHA != commitC || stored.baseSHA != commitB ||
		stored.state != GitHubPullRequestOpen || stored.mergeability != GitHubMergeabilityUnknown ||
		stored.lastSettledMergeability != GitHubConflicting || stored.eventSequence != firstSequence[0] {
		t.Errorf("pending observation = %+v, want updated revisions, UNKNOWN state, preserved conflict and sequence", stored)
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
			BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB,
			State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
		},
	}); err != nil {
		t.Fatal(err)
	}
	if got := g.mergeabilitySequences(t, 7); !slices.Equal(got, firstSequence) {
		t.Errorf("CONFLICTING → UNKNOWN → CONFLICTING event sequences = %v, want no duplicate after %v", got, firstSequence)
	}
}

func TestInitiallyMergeablePullRequestDoesNotEmitUntilItConflicts(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeable,
	})
	if got := g.mergeabilitySequences(t, 7); len(got) != 0 {
		t.Fatalf("initial MERGEABLE observation sequences = %v, want none", got)
	}
	g.reconcileAll(t, sub)
	if entries := mergeabilityEntries(t, g); len(entries) != 0 {
		t.Fatalf("entries after initial MERGEABLE observation = %v, want none", entries)
	}

	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
	})
	sequences := g.mergeabilitySequences(t, 7)
	if len(sequences) != 1 {
		t.Fatalf("initial conflict after MERGEABLE sequences = %v, want one", sequences)
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
	observations := []GitHubPullRequestObservation{
		{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubConflicting},
		{BaseRef: "main", HeadSHA: commitB, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeable},
		{BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubConflicting},
	}
	for _, observation := range observations {
		g.observePullRequest(t, defaultOne, observation)
		g.reconcileAll(t, defaultOne, defaultTwo, explicitComments)
	}

	sequences := g.mergeabilitySequences(t, 7)
	if len(sequences) != 3 || !(sequences[0] < sequences[1] && sequences[1] < sequences[2]) {
		t.Fatalf("CONFLICTING → MERGEABLE → CONFLICTING event sequences = %v, want three increasing transitions", sequences)
	}
	entries := mergeabilityEntries(t, g)
	if len(entries) != 3 {
		t.Fatalf("mergeability entries = %d, want one per transition: %+v", len(entries), entries)
	}
	wantStates := []struct {
		previous GitHubMergeableState
		current  GitHubMergeableState
	}{
		{GitHubMergeabilityUnknown, GitHubConflicting},
		{GitHubConflicting, GitHubMergeable},
		{GitHubMergeable, GitHubConflicting},
	}
	for i, entry := range entries {
		event, ok := entry.Event.(GitHubMergeabilityEvent)
		if !ok || event.Sequence != sequences[i] || event.AppID != g.app || event.RepositoryID != g.repository || event.PullRequestNumber != 7 {
			t.Errorf("entry %d event = %#v, want transition sequence %d for this pull request", i, entry.Event, sequences[i])
		}
		if want := sortedIDs(defaultOne.ID, defaultTwo.ID); !slices.Equal(entry.SubscriptionIDs, want) {
			t.Errorf("entry %d subscriptions = %v, want both default subscribers %v", i, entry.SubscriptionIDs, want)
		}
		var payload struct {
			PreviousMergeability GitHubMergeableState `json:"previousMergeability"`
			Mergeability         GitHubMergeableState `json:"mergeability"`
		}
		if err := json.Unmarshal(entry.Payload, &payload); err != nil {
			t.Errorf("entry %d mergeability payload: %v", i, err)
		} else if payload.PreviousMergeability != wantStates[i].previous || payload.Mergeability != wantStates[i].current {
			t.Errorf("entry %d payload states = %s → %s, want %s → %s", i,
				payload.PreviousMergeability, payload.Mergeability, wantStates[i].previous, wantStates[i].current)
		}
	}
	if got := sequencesForSubscription(t, entries, explicitComments.ID); len(got) != 0 {
		t.Errorf("explicit comments filter received mergeability sequences %v", got)
	}
}

func TestNewSubscriberToCachedConflictGetsOnlyTheCurrentConflict(t *testing.T) {
	g := newGitHubFixture(t)
	seed := g.follow(t, g.source(PullRequestSubject{Number: 7}), "seed")
	for _, observation := range []GitHubPullRequestObservation{
		{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubConflicting},
		{BaseRef: "main", HeadSHA: commitB, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeable},
		{BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubConflicting},
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
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
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
			BaseRef: "main", HeadSHA: commitC, BaseSHA: commitC,
			State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
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
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
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
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
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

func TestTerminalPullRequestsDoNotEmitMergeabilityEventsOrRetry(t *testing.T) {
	for _, terminal := range []struct {
		name  string
		state GitHubPullRequestState
	}{
		{name: "closed", state: GitHubPullRequestClosed},
		{name: "merged", state: GitHubPullRequestMerged},
	} {
		t.Run(terminal.name+"/initial", func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
			g.observePullRequest(t, sub, GitHubPullRequestObservation{
				BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
				State: terminal.state, Mergeability: GitHubMergeabilityUnknown,
			})
			stored := g.pullRequestObservation(t, 7)
			if stored.state != terminal.state || stored.mergeability != GitHubMergeabilityUnknown ||
				stored.lastSettledMergeability != GitHubMergeabilityUnknown || stored.eventSequence != 0 {
				t.Errorf("initial terminal observation = %+v, want persisted %s state with no settled result or event", stored, terminal.state)
			}
			if retry := g.pullRequestRetryAt(t, sub); retry.Valid {
				t.Errorf("terminal observation retry = %v, want no pending retry", retry)
			}
			g.reconcileAll(t, sub)
			if got := g.mergeabilitySequences(t, 7); len(got) != 0 || len(mergeabilityEntries(t, g)) != 0 {
				t.Fatalf("initial %s observation events/entries = %v/%v, want none", terminal.state, got, mergeabilityEntries(t, g))
			}
		})

		t.Run(terminal.name+"/ends-pending", func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")

			g.observePullRequest(t, sub, GitHubPullRequestObservation{
				BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
				State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
			})
			openSequences := g.mergeabilitySequences(t, 7)
			if len(openSequences) != 1 {
				t.Fatalf("open conflict events = %v, want one", openSequences)
			}
			g.reconcileAll(t, sub)
			g.observePullRequest(t, sub, GitHubPullRequestObservation{
				BaseRef: "main", HeadSHA: commitB, BaseSHA: commitB,
				State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
			})
			if retry := g.pullRequestRetryAt(t, sub); !retry.Valid || !retry.Time.After(time.Now()) {
				t.Fatalf("pending observation retry = %v, want a future deadline", retry)
			}

			g.observePullRequest(t, sub, GitHubPullRequestObservation{
				BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
				State: terminal.state, Mergeability: GitHubMergeabilityUnknown,
			})
			stored := g.pullRequestObservation(t, 7)
			if stored.state != terminal.state || stored.mergeability != GitHubMergeabilityUnknown ||
				stored.lastSettledMergeability != GitHubConflicting || stored.eventSequence != openSequences[0] {
				t.Errorf("terminal transition observation = %+v, want %s, UNKNOWN and retained conflict", stored, terminal.state)
			}
			if retry := g.pullRequestRetryAt(t, sub); retry.Valid {
				t.Errorf("terminal transition retry = %v, want no pending retry", retry)
			}
			g.reconcileAll(t, sub)
			if got := g.mergeabilitySequences(t, 7); !slices.Equal(got, openSequences) {
				t.Errorf("event sequences after %s = %v, want only the open conflict %v", terminal.state, got, openSequences)
			}
			if entries := mergeabilityEntries(t, g); len(entries) != 1 {
				t.Errorf("entries after %s = %v, want only the open conflict", terminal.state, entries)
			}
		})
	}
}

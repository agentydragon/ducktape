package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

type storedPullRequestObservation struct {
	baseRef                 string
	headSHA                 string
	baseSHA                 string
	mergeCommitSHA          sql.NullString
	state                   GitHubPullRequestState
	mergeability            GitHubMergeableState
	lastSettledMergeability GitHubMergeableState
	recheckAttempt          int
	snapshotVersion         int64
	observedAt              time.Time
}

func (g githubFixture) pullRequestObservation(t *testing.T, number int64) storedPullRequestObservation {
	t.Helper()
	var observation storedPullRequestObservation
	if err := g.db.QueryRowContext(t.Context(), `SELECT base_ref, head_sha, base_sha, merge_commit_sha, state, mergeability,
		last_settled_mergeability, mergeability_recheck_attempt, snapshot_version, observed_at
		FROM github_pull_request WHERE repository_id = $1 AND number = $2`, g.repository, number).Scan(
		&observation.baseRef, &observation.headSHA, &observation.baseSHA, &observation.mergeCommitSHA,
		&observation.state, &observation.mergeability,
		&observation.lastSettledMergeability, &observation.recheckAttempt, &observation.snapshotVersion, &observation.observedAt,
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
	if err := g.tryObservePullRequest(t, sub, observation); err != nil {
		t.Fatal(err)
	}
}

func (g githubFixture) tryObservePullRequest(t *testing.T, sub githubSubscription, observation GitHubPullRequestObservation) error {
	t.Helper()
	g.forcePullRequestRefreshDue(t, sub)
	return g.tryCompletePullRequestRefresh(t, sub, observation)
}

func (g githubFixture) tryCompletePullRequestRefresh(t *testing.T, sub githubSubscription, observation GitHubPullRequestObservation) error {
	t.Helper()
	lease, err := g.state.Acquire(t.Context(), sub.Subject)
	if err != nil || lease == nil {
		t.Fatalf("pull request refresh lease = %v, %v", lease, err)
	}
	base := g.validate(t, g.access())
	return g.state.Succeed(t.Context(), *lease, GitHubSuccess{
		Fences: []GitHubAccessFence{base}, PullRequest: &observation,
		Revisions: []GitHubHeadRevision{{Repository: GitHubRepository{ID: g.repository, Name: testRepositoryName}, SHA: observation.HeadSHA}},
	})
}

func testMergeCommit(baseSHA, headSHA string) *GitHubMergeCommit {
	return &GitHubMergeCommit{SHA: strings.Repeat("d", 40), BaseSHA: baseSHA, HeadSHA: headSHA}
}

func (g githubFixture) assertPullRequestRetry(t *testing.T, sub githubSubscription, wantDelay time.Duration, wantAttempt int) {
	t.Helper()
	stored := g.pullRequestObservation(t, 7)
	if stored.recheckAttempt != wantAttempt {
		t.Errorf("mergeability retry attempt = %d, want %d", stored.recheckAttempt, wantAttempt)
	}
	retry := g.pullRequestRetryAt(t, sub)
	if !retry.Valid {
		t.Fatalf("mergeability retry deadline is unset, want %v", wantDelay)
	}
	if got := retry.Time.Sub(stored.observedAt); got != wantDelay {
		t.Errorf("mergeability retry delay = %v, want %v", got, wantDelay)
	}
}

func (g githubFixture) assertNoPullRequestRetry(t *testing.T, sub githubSubscription, wantAttempt int) {
	t.Helper()
	stored := g.pullRequestObservation(t, 7)
	if stored.recheckAttempt != wantAttempt {
		t.Errorf("mergeability retry attempt = %d, want %d", stored.recheckAttempt, wantAttempt)
	}
	if retry := g.pullRequestRetryAt(t, sub); retry.Valid {
		t.Errorf("mergeability retry deadline = %v, want ordinary freshness only", retry)
	}
	state, err := g.state.Subject(t.Context(), sub.Subject)
	if err != nil {
		t.Fatal(err)
	}
	if state.LastSuccessAt == nil || !state.RepairAt.Equal(state.LastSuccessAt.Add(testFreshness)) {
		t.Errorf("subject repair state = %+v, want ordinary %v freshness", state, testFreshness)
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

func mergeabilitySnapshotEntries(t *testing.T, g githubFixture, inboxID uuid.UUID) []Entry {
	t.Helper()
	var entries []Entry
	for _, entry := range g.read(t, inboxID, 0).Entries {
		if _, ok := entry.Event.(GitHubMergeabilitySnapshotEvent); ok {
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

func (g githubFixture) reconcileSubscription(t *testing.T, sub githubSubscription) {
	t.Helper()
	if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
		t.Fatal(err)
	}
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
		stored.lastSettledMergeability != GitHubConflicting || stored.snapshotVersion != 2 || stored.recheckAttempt != 1 {
		t.Errorf("pending observation = %+v, want updated revisions, UNKNOWN state, preserved conflict and sequence", stored)
	}
	g.assertPullRequestRetry(t, sub, 5*time.Second, 1)

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
	g.assertPullRequestRetry(t, sub, 5*time.Second, 1)
	if got := g.mergeabilitySequences(t, 7); !slices.Equal(got, firstSequence) {
		t.Errorf("CONFLICTING → UNKNOWN → CONFLICTING event sequences = %v, want no duplicate after %v", got, firstSequence)
	}
}

func TestUnknownRetryAttemptSurvivesReacquireWebhookAndResetsOnRefChange(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	observation := GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	}
	g.observePullRequest(t, sub, observation)
	g.assertPullRequestRetry(t, sub, 5*time.Second, 1)

	// Process restart preserves both the pending deadline and the tuple's next retry step.
	g.state = NewGitHubState(g.Store, testFreshness)
	lease, err := g.state.Acquire(t.Context(), sub.Subject)
	if lease != nil || err != nil {
		t.Fatalf("refresh before pending deadline = %v, %v; want no lease", lease, err)
	}
	g.exec(t, `UPDATE github_subject SET next_attempt = now() - interval '1 second'
		WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`, sub.Subject.RepositoryID, sub.Subject.Kind, sub.Subject.Key)
	if err := g.tryCompletePullRequestRefresh(t, sub, observation); err != nil {
		t.Fatal(err)
	}
	g.assertPullRequestRetry(t, sub, 15*time.Second, 2)

	// An unchanged pull_request webhook forces a read, but the unchanged observation keeps its retry budget.
	g.deliver(t, "pull_request", onPullRequest(7), withAction("edited"))
	if err := g.tryCompletePullRequestRefresh(t, sub, observation); err != nil {
		t.Fatal(err)
	}
	g.assertPullRequestRetry(t, sub, 45*time.Second, 3)

	// A changed head starts this observed tuple at the first delay again.
	g.deliver(t, "pull_request", onPullRequest(7), withAction("synchronize"))
	changed := observation
	changed.HeadSHA = commitC
	if err := g.tryCompletePullRequestRefresh(t, sub, changed); err != nil {
		t.Fatal(err)
	}
	g.assertPullRequestRetry(t, sub, 5*time.Second, 1)
}

func TestUnknownRetryScheduleCapsAtSixtySecondsAndSettledResultsResetIt(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	unknown := GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	}
	want := []struct {
		delay   time.Duration
		attempt int
	}{
		{5 * time.Second, 1},
		{15 * time.Second, 2},
		{45 * time.Second, 3},
		{time.Minute, 4},
		{time.Minute, 4},
	}
	for i, step := range want {
		g.observePullRequest(t, sub, unknown)
		if stored := g.pullRequestObservation(t, 7); stored.mergeability != GitHubMergeabilityUnknown {
			t.Fatalf("unknown retry step %d stored mergeability = %s", i, stored.mergeability)
		}
		g.assertPullRequestRetry(t, sub, step.delay, step.attempt)
	}

	conflicting := unknown
	conflicting.Mergeability = GitHubConflicting
	g.observePullRequest(t, sub, conflicting)
	g.assertPullRequestRetry(t, sub, 5*time.Second, 1)
	if got := g.mergeabilitySequences(t, 7); len(got) != 1 {
		t.Errorf("UNKNOWN → CONFLICTING event sequences = %v, want the negative transition despite retry scheduling", got)
	}

	mergeable := conflicting
	mergeable.Mergeability = GitHubMergeable
	mergeable.MergeCommit = testMergeCommit(mergeable.BaseSHA, mergeable.HeadSHA)
	g.observePullRequest(t, sub, mergeable)
	g.assertNoPullRequestRetry(t, sub, 0)
}

func TestConflictingRetryScheduleStopsAfterThreeEarlyRechecks(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	conflicting := GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
	}
	for _, step := range []struct {
		delay   time.Duration
		attempt int
	}{
		{5 * time.Second, 1},
		{15 * time.Second, 2},
		{45 * time.Second, 3},
	} {
		g.observePullRequest(t, sub, conflicting)
		g.assertPullRequestRetry(t, sub, step.delay, step.attempt)
	}
	// Once the unchanged conflict exhausts its short retry budget, source freshness controls the next read.
	g.observePullRequest(t, sub, conflicting)
	g.assertNoPullRequestRetry(t, sub, 3)

	// An unchanged webhook reacquires the subject without resetting the exhausted tuple's counter.
	g.deliver(t, "pull_request", onPullRequest(7), withAction("edited"))
	if err := g.tryCompletePullRequestRefresh(t, sub, conflicting); err != nil {
		t.Fatal(err)
	}
	g.assertNoPullRequestRetry(t, sub, 3)

	// A changed base ref starts a new negative retry budget.
	g.deliver(t, "pull_request", onPullRequest(7), withAction("edited"))
	changed := conflicting
	changed.BaseRef = "release"
	if err := g.tryCompletePullRequestRefresh(t, sub, changed); err != nil {
		t.Fatal(err)
	}
	g.assertPullRequestRetry(t, sub, 5*time.Second, 1)
}

func TestInitiallyMergeablePullRequestDoesNotEmitUntilItConflicts(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeable, MergeCommit: testMergeCommit(commitB, commitA),
	})
	stored := g.pullRequestObservation(t, 7)
	if !stored.mergeCommitSHA.Valid || stored.mergeCommitSHA.String != testMergeCommit(commitB, commitA).SHA ||
		stored.baseSHA != commitB || stored.headSHA != commitA {
		t.Errorf("initial verified MERGEABLE observation = %+v, want proof against current refs", stored)
	}
	g.assertNoPullRequestRetry(t, sub, 0)
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

func TestMergeablePullRequestRequiresMatchingTestMergeProof(t *testing.T) {
	tests := []struct {
		name  string
		proof *GitHubMergeCommit
	}{
		{name: "missing"},
		{name: "wrong-base", proof: &GitHubMergeCommit{SHA: strings.Repeat("e", 40), BaseSHA: commitC, HeadSHA: commitA}},
		{name: "wrong-head", proof: &GitHubMergeCommit{SHA: strings.Repeat("e", 40), BaseSHA: commitB, HeadSHA: commitC}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
			err := g.tryObservePullRequest(t, sub, GitHubPullRequestObservation{
				BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
				State: GitHubPullRequestOpen, Mergeability: GitHubMergeable, MergeCommit: test.proof,
			})
			if err == nil {
				t.Fatal("accepted MERGEABLE observation without a matching test merge")
			}
			var count int
			if err := g.db.QueryRowContext(t.Context(), `SELECT count(*) FROM github_pull_request
				WHERE repository_id = $1 AND number = 7`, g.repository).Scan(&count); err != nil {
				t.Fatal(err)
			}
			if count != 0 {
				t.Errorf("rejected observation persisted %d pull request rows", count)
			}
			if got := g.mergeabilitySequences(t, 7); len(got) != 0 {
				t.Errorf("rejected observation generated transition sequences %v", got)
			}
		})
	}
}

func TestMergeabilityTransitionsUseDefaultFiltersAndDeduplicateSubscribers(t *testing.T) {
	g := newGitHubFixture(t)
	defaultOne := g.follow(t, g.source(PullRequestSubject{Number: 7}), "default-one")
	defaultTwo := g.follow(t, g.source(PullRequestSubject{Number: 7}), "default-two")
	explicitComments := g.follow(t, g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventIssueComment}), "comments")
	observations := []GitHubPullRequestObservation{
		{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubConflicting},
		{
			BaseRef: "main", HeadSHA: commitB, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeable,
			MergeCommit: testMergeCommit(commitB, commitB),
		},
		{BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubConflicting},
	}
	for _, observation := range observations {
		g.observePullRequest(t, defaultOne, observation)
		if observation.Mergeability == GitHubMergeable {
			wantProof := testMergeCommit(observation.BaseSHA, observation.HeadSHA)
			stored := g.pullRequestObservation(t, 7)
			if !stored.mergeCommitSHA.Valid || stored.mergeCommitSHA.String != wantProof.SHA ||
				stored.baseSHA != wantProof.BaseSHA || stored.headSHA != wantProof.HeadSHA {
				t.Errorf("stored MERGEABLE proof = %+v, want matching pair and sha %s", stored, wantProof.SHA)
			}
		}
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
	wantRefs := []struct {
		base string
		head string
	}{
		{base: commitB, head: commitA},
		{base: commitB, head: commitB},
		{base: commitB, head: commitC},
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
			BaseSHA              string               `json:"baseSha"`
			HeadSHA              string               `json:"headSha"`
			MergeCommit          *GitHubMergeCommit   `json:"mergeCommit"`
		}
		if err := json.Unmarshal(entry.Payload, &payload); err != nil {
			t.Errorf("entry %d mergeability payload: %v", i, err)
		} else if payload.PreviousMergeability != wantStates[i].previous || payload.Mergeability != wantStates[i].current {
			t.Errorf("entry %d payload states = %s → %s, want %s → %s", i,
				payload.PreviousMergeability, payload.Mergeability, wantStates[i].previous, wantStates[i].current)
		} else if payload.BaseSHA != wantRefs[i].base || payload.HeadSHA != wantRefs[i].head {
			t.Errorf("entry %d payload refs = %s/%s, want observed pair %s/%s", i,
				payload.BaseSHA, payload.HeadSHA, wantRefs[i].base, wantRefs[i].head)
		} else if i == 1 {
			wantProof := testMergeCommit(commitB, commitB)
			if payload.BaseSHA != commitB || payload.HeadSHA != commitB || payload.MergeCommit == nil || *payload.MergeCommit != *wantProof {
				t.Errorf("verified MERGEABLE payload = refs %s/%s, proof %#v; want refs %s/%s and proof %#v",
					payload.BaseSHA, payload.HeadSHA, payload.MergeCommit, commitB, commitB, wantProof)
			}
		} else if payload.MergeCommit != nil {
			t.Errorf("unverified %s transition unexpectedly carries merge proof %#v", payload.Mergeability, payload.MergeCommit)
		}
	}
	if got := sequencesForSubscription(t, entries, explicitComments.ID); len(got) != 0 {
		t.Errorf("explicit comments filter received mergeability sequences %v", got)
	}
	stored := g.pullRequestObservation(t, 7)
	if stored.mergeCommitSHA.Valid {
		t.Errorf("final CONFLICTING snapshot retained stale merge proof %q", stored.mergeCommitSHA.String)
	}
}

func TestLateSubscriberGetsCurrentConflictSnapshotAfterHeadChanges(t *testing.T) {
	g := newGitHubFixture(t)
	seed := g.follow(t, g.source(PullRequestSubject{Number: 7}), "seed")
	g.observePullRequest(t, seed, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
	})
	g.reconcileAll(t, seed)
	oldVersion := g.pullRequestObservation(t, 7).snapshotVersion
	g.observePullRequest(t, seed, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
	})
	current := g.pullRequestObservation(t, 7)
	if current.snapshotVersion != oldVersion+1 || current.headSHA != commitC {
		t.Fatalf("current snapshot = %+v, want a new version for head %s", current, commitC)
	}
	if got := g.mergeabilitySequences(t, 7); len(got) != 1 {
		t.Fatalf("same conflict at a new head generated transition sequences %v, want only the original conflict", got)
	}

	destination := g.destination
	destination.Name = "late-subscriber"
	newSubscriber := g.followIn(t, destination, g.source(PullRequestSubject{Number: 7}), "late-subscriber")
	g.reconcileSubscription(t, newSubscriber)
	allEntries := g.read(t, newSubscriber.InboxID, 0).Entries
	if len(allEntries) != 2 {
		t.Fatalf("new subscriber received %d total entries, want current conflict and lifecycle snapshots", len(allEntries))
	}
	lifecycle := prStateObservationEntries(t, allEntries)
	if len(lifecycle) != 1 {
		t.Fatalf("lifecycle snapshots = %d, want one", len(lifecycle))
	}
	var currentLifecycle GitHubPRLifecycleObservation
	if err := json.Unmarshal(lifecycle[0].Payload, &currentLifecycle); err != nil {
		t.Fatal(err)
	}
	if currentLifecycle.HeadSHA != commitC {
		t.Fatalf("lifecycle snapshot head = %s, want %s", currentLifecycle.HeadSHA, commitC)
	}
	entries := mergeabilitySnapshotEntries(t, g, newSubscriber.InboxID)
	if len(entries) != 1 {
		t.Fatalf("new subscriber snapshot entries = %v, want one current conflict snapshot", entries)
	}
	event, ok := entries[0].Event.(GitHubMergeabilitySnapshotEvent)
	if !ok || event.Version != current.snapshotVersion || event.AppID != g.app || event.RepositoryID != g.repository || event.PullRequestNumber != 7 {
		t.Errorf("snapshot event = %#v, want current snapshot version %d", entries[0].Event, current.snapshotVersion)
	}
	var payload struct {
		HeadSHA      string               `json:"headSha"`
		Mergeability GitHubMergeableState `json:"mergeability"`
	}
	if err := json.Unmarshal(entries[0].Payload, &payload); err != nil {
		t.Fatal(err)
	}
	if payload.HeadSHA != commitC || payload.Mergeability != GitHubConflicting {
		t.Errorf("current conflict payload = head %s / %s, want head %s / CONFLICTING", payload.HeadSHA, payload.Mergeability, commitC)
	}
	if got := sequencesForSubscription(t, mergeabilityEntries(t, g), newSubscriber.ID); len(got) != 0 {
		t.Errorf("late subscriber replayed transition history %v", got)
	}
}

func TestNewInstallationGetsInitialConflictSnapshot(t *testing.T) {
	g := newGitHubFixture(t)
	seed := g.follow(t, g.source(PullRequestSubject{Number: 7}), "seed")
	observation := GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
	}
	g.observePullRequest(t, seed, observation)
	g.reconcileAll(t, seed)
	version := g.pullRequestObservation(t, 7).snapshotVersion

	// A new installation identity gets its own access fence and must observe the current conflict.
	g.installation += 100
	destination := g.destination
	destination.Name = "new-installation-subscriber"
	recreated := g.followIn(t, destination, g.source(PullRequestSubject{Number: 7}), "recreated")
	g.observePullRequest(t, recreated, observation)
	g.reconcileSubscription(t, recreated)
	if got := len(g.read(t, recreated.InboxID, 0).Entries); got != 1 {
		t.Fatalf("new installation received %d total entries, want only the current snapshot", got)
	}
	entries := mergeabilitySnapshotEntries(t, g, recreated.InboxID)
	if len(entries) != 1 {
		t.Fatalf("new installation snapshot entries = %v, want one", entries)
	}
	event, ok := entries[0].Event.(GitHubMergeabilitySnapshotEvent)
	if !ok || event.Version != version {
		t.Errorf("new installation snapshot event = %#v, want shared current version %d", entries[0].Event, version)
	}
}

func TestIdenticalObservationKeepsSnapshotVersionAndOverlappingSubscribersDeduplicate(t *testing.T) {
	g := newGitHubFixture(t)
	seed := g.follow(t, g.source(PullRequestSubject{Number: 7}), "seed")
	observation := GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
	}
	g.observePullRequest(t, seed, observation)
	g.reconcileAll(t, seed)
	before := g.pullRequestObservation(t, 7)

	first := g.follow(t, g.source(PullRequestSubject{Number: 7}), "late-overlap-one")
	second := g.follow(t, g.source(PullRequestSubject{Number: 7}), "late-overlap-two")
	// Age the stored timestamp so the next identical API read proves observed_at is refreshed.
	g.exec(t, `UPDATE github_pull_request SET observed_at = now() - interval '1 hour'
		WHERE repository_id = $1 AND number = 7`, g.repository)
	g.observePullRequest(t, first, observation)
	after := g.pullRequestObservation(t, 7)
	if after.snapshotVersion != before.snapshotVersion {
		t.Errorf("identical settled observation version = %d, want unchanged version %d", after.snapshotVersion, before.snapshotVersion)
	}
	if !after.observedAt.After(before.observedAt) {
		t.Errorf("identical settled observation observed_at = %v, want later than %v", after.observedAt, before.observedAt)
	}
	if got := g.mergeabilitySequences(t, 7); len(got) != 1 {
		t.Errorf("identical settled observation transition history = %v, want only the initial conflict", got)
	}

	g.reconcileAll(t, first, second)
	entries := mergeabilitySnapshotEntries(t, g, g.inbox)
	if len(entries) != 1 {
		t.Fatalf("overlapping initial snapshot entries = %v, want one deduplicated entry", entries)
	}
	if want := sortedIDs(first.ID, second.ID); !slices.Equal(entries[0].SubscriptionIDs, want) {
		t.Errorf("snapshot subscription IDs = %v, want %v", entries[0].SubscriptionIDs, want)
	}
	event, ok := entries[0].Event.(GitHubMergeabilitySnapshotEvent)
	if !ok || event.Version != before.snapshotVersion {
		t.Errorf("deduplicated snapshot identity = %#v, want version %d", entries[0].Event, before.snapshotVersion)
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
	g.forcePullRequestRefreshDue(t, sub)
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

func TestRetargetedPullRequestBasePushFencesInFlightRefresh(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "follow")
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeable, MergeCommit: testMergeCommit(commitB, commitA),
	})
	// The PR webhook schedules a refresh, but the saved snapshot still names main.
	g.deliver(t, "pull_request", onPullRequest(7), withAction("edited"))
	lease, err := g.state.Acquire(t.Context(), sub.Subject)
	if err != nil || lease == nil {
		t.Fatalf("retarget refresh lease = %v, %v", lease, err)
	}
	// A release push arrives while the API read is in flight and the read discovers the retarget.
	g.deliver(t, "push", onBranch("release"), atCommit(commitC))
	err = g.state.Succeed(t.Context(), *lease, GitHubSuccess{
		Fences: []GitHubAccessFence{g.validate(t, g.access())},
		PullRequest: &GitHubPullRequestObservation{
			BaseRef: "release", HeadSHA: commitA, BaseSHA: commitB,
			State: GitHubPullRequestOpen, Mergeability: GitHubMergeable, MergeCommit: testMergeCommit(commitB, commitA),
		},
	})
	var deferred *RefreshDeferredError
	if !errors.As(err, &deferred) {
		t.Fatalf("stale result for retargeted base was accepted: error=%v; want a fenced refresh", err)
	}
	stored := g.pullRequestObservation(t, 7)
	if stored.baseRef != "main" || stored.snapshotVersion != 1 {
		t.Errorf("stale retargeted result changed snapshot: %+v", stored)
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
				stored.lastSettledMergeability != GitHubMergeabilityUnknown || stored.snapshotVersion != 1 || stored.recheckAttempt != 0 {
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
				stored.lastSettledMergeability != GitHubConflicting || stored.snapshotVersion != 3 || stored.recheckAttempt != 0 {
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

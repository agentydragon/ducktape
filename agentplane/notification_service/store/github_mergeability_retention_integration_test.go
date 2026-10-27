package store

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestCleanupGitHubDeliveriesRetainsSnapshotsAndInboxCopies(t *testing.T) {
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
	seedTransitions := mergeabilityEntries(t, g)
	if len(seedTransitions) != 3 {
		t.Fatalf("seed transition entries = %d, want three before retention", len(seedTransitions))
	}

	// A same-state observation advances the current snapshot, but creates no extra transition row.
	currentHead := strings.Repeat("d", 40)
	g.observePullRequest(t, seed, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: currentHead, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
	})
	current := g.pullRequestObservation(t, 7)
	if current.headSHA != currentHead {
		t.Fatalf("current snapshot head = %s, want %s", current.headSHA, currentHead)
	}

	// Keep a fresh generated transition alongside the old PR 7 history.
	fresh := g.follow(t, g.source(PullRequestSubject{Number: 8}), "fresh")
	g.observePullRequest(t, fresh, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubConflicting,
	})
	if got := g.mergeabilitySequences(t, 8); len(got) != 1 {
		t.Fatalf("fresh PR transition sequences = %v, want one", got)
	}

	cutoff := time.Now().Add(-24 * time.Hour)
	g.exec(t, `UPDATE github_mergeability_event SET created_at = $2
		WHERE app_id = $1 AND repository_id = $3 AND pull_request_number = 7`, g.app, cutoff.Add(-time.Hour), g.repository)
	if err := g.CleanupGitHubDeliveries(t.Context(), cutoff); err != nil {
		t.Fatal(err)
	}
	if got := g.mergeabilitySequences(t, 7); len(got) != 0 {
		t.Errorf("expired PR transition history after cleanup = %v, want none", got)
	}
	if got := g.mergeabilitySequences(t, 8); len(got) != 1 {
		t.Errorf("fresh PR transition history after cleanup = %v, want one retained event", got)
	}
	afterCleanup := g.pullRequestObservation(t, 7)
	if afterCleanup.headSHA != currentHead || afterCleanup.snapshotVersion != current.snapshotVersion ||
		afterCleanup.mergeability != GitHubConflicting {
		t.Errorf("current snapshot after cleanup = %+v, want preserved current conflict at %s", afterCleanup, currentHead)
	}
	if got := mergeabilityEntries(t, g); len(got) != len(seedTransitions) {
		t.Errorf("seed inbox transition copies after cleanup = %d, want all %d retained", len(got), len(seedTransitions))
	}

	// The current conflict remains available for an initial notice after every transition row has expired.
	destination := g.destination
	destination.Name = "after-retention"
	late := g.followIn(t, destination, g.source(PullRequestSubject{Number: 7}), "after-retention")
	g.reconcileSubscription(t, late)
	if got := len(g.read(t, late.InboxID, 0).Entries); got != 1 {
		t.Fatalf("late subscriber received %d total entries after retention, want only the current snapshot", got)
	}
	entries := mergeabilitySnapshotEntries(t, g, late.InboxID)
	if len(entries) != 1 {
		t.Fatalf("late subscriber snapshot entries after retention = %v, want one", entries)
	}
	event, ok := entries[0].Event.(GitHubMergeabilitySnapshotEvent)
	if !ok || event.Version != current.snapshotVersion {
		t.Errorf("late snapshot identity = %#v, want current version %d", entries[0].Event, current.snapshotVersion)
	}
	var payload struct {
		HeadSHA      string               `json:"headSha"`
		Mergeability GitHubMergeableState `json:"mergeability"`
	}
	if err := json.Unmarshal(entries[0].Payload, &payload); err != nil {
		t.Fatal(err)
	}
	if payload.HeadSHA != currentHead || payload.Mergeability != GitHubConflicting {
		t.Errorf("late snapshot payload = %s / %s, want %s / CONFLICTING", payload.HeadSHA, payload.Mergeability, currentHead)
	}
}

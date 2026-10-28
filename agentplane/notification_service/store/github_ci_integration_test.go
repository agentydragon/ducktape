package store

import (
	"bytes"
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
)

func checkRunObservation(g githubFixture, sha, status, conclusion string) GitHubCIObservation {
	action := "created"
	if status == "in_progress" {
		action = "in_progress"
	} else if status == "completed" {
		action = "completed"
	}
	observation := GitHubCIObservation{
		RepositoryID: g.repository, HeadSHA: sha, Event: EventCheckRun, Action: &action,
		CheckAppID: 202, ID: 303, Name: "unit", Status: status,
	}
	if conclusion != "" {
		observation.Conclusion = &conclusion
	}
	return observation
}

func (g githubFixture) pollCI(t *testing.T, subject GitHubSubjectKey, observation GitHubCIObservation) {
	t.Helper()
	key := GitHubCIRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: subject}
	lease, err := g.state.Acquire(t.Context(), key)
	if err != nil || lease == nil {
		t.Fatalf("CI refresh lease = %v, %v", lease, err)
	}
	if err := g.state.Succeed(t.Context(), *lease, GitHubSuccess{
		Fences: []GitHubAccessFence{g.validate(t, g.access())}, CI: []GitHubCIObservation{observation},
	}); err != nil {
		t.Fatal(err)
	}
}

func normalizedCheckRunDelivery(g githubFixture, sha string, observation GitHubCIObservation) GitHubIngest {
	options := []deliveryOption{onPullRequest(7), atCommit(sha)}
	if observation.Action != nil {
		options = append([]deliveryOption{action(*observation.Action)}, options...)
	}
	ingest := g.delivery(string(EventCheckRun), options...)
	ingest.CI = []GitHubCIObservation{observation}
	return ingest
}

func observationEntries(t *testing.T, entries []Entry) []Entry {
	t.Helper()
	var observations []Entry
	for _, entry := range entries {
		if _, ok := entry.Event.(GitHubObservationEvent); ok {
			observations = append(observations, entry)
		}
	}
	return observations
}

func TestGitHubCIPollAndWebhookShareTransitionsAndNewSubscribersGetCurrentState(t *testing.T) {
	for _, webhookFirst := range []bool{true, false} {
		name := "poll first"
		if webhookFirst {
			name = "webhook first"
		}
		t.Run(name, func(t *testing.T) {
			g := newGitHubFixture(t)
			source := g.source(PullRequestSubject{Number: 7})
			first := g.follow(t, source, "first")
			g.observePullRequest(t, first, GitHubPullRequestObservation{
				BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
				State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
			})
			second := g.follow(t, source, "second")
			observation := checkRunObservation(g, commitA, "completed", "success")
			pollObservation := observation
			pollObservation.DetailsURL = "https://checks.example/current"
			updatedAt := time.Now().UTC()
			pollObservation.UpdatedAt = &updatedAt
			if webhookFirst {
				g.ingest(t, normalizedCheckRunDelivery(g, commitA, observation))
				g.pollCI(t, first.Subject, pollObservation)
			} else {
				g.pollCI(t, first.Subject, pollObservation)
				g.ingest(t, normalizedCheckRunDelivery(g, commitA, observation))
			}

			if got := g.count(t, `SELECT count(*) FROM github_ci_event WHERE app_id = $1`, g.app); got != 1 {
				t.Fatalf("durable normalized transitions = %d, want one", got)
			}
			for _, sub := range []githubSubscription{first, second} {
				if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
					t.Fatal(err)
				}
			}
			entries := observationEntries(t, g.read(t, g.inbox, 0).Entries)
			if len(entries) != 1 {
				t.Fatalf("shared inbox normalized observations = %d, want one", len(entries))
			}
			if got, want := entries[0].SubscriptionIDs, []uuid.UUID{first.ID, second.ID}; !sameUUIDs(got, want) {
				t.Errorf("shared event subscriptions = %v, want %v", got, want)
			}
			var payload GitHubCIObservation
			if err := json.Unmarshal(entries[0].Payload, &payload); err != nil {
				t.Fatal(err)
			}
			if payload.ID != observation.ID || payload.HeadSHA != commitA || payload.Status != "completed" || payload.Conclusion == nil || *payload.Conclusion != "success" {
				t.Errorf("normalized event payload = %+v", payload)
			}
			var eventPayload, currentPayload []byte
			if err := g.db.QueryRowContext(t.Context(), `SELECT payload FROM github_ci_event WHERE app_id = $1 ORDER BY sequence LIMIT 1`, g.app).Scan(&eventPayload); err != nil {
				t.Fatal(err)
			}
			if err := g.db.QueryRowContext(t.Context(), `SELECT payload FROM github_ci_current WHERE app_id = $1 AND head_sha = $2`, g.app, commitA).Scan(&currentPayload); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(eventPayload, currentPayload) {
				t.Errorf("immutable event payload %s differs from current snapshot %s", eventPayload, currentPayload)
			}

			lateDestination := g.destination
			lateDestination.Name = "late-subscriber"
			late := g.followIn(t, lateDestination, source, "late")
			g.reconcileSubscription(t, late)
			lateEntries := observationEntries(t, g.read(t, late.InboxID, 0).Entries)
			if len(lateEntries) != 1 {
				t.Fatalf("late current-state observations = %d, want one", len(lateEntries))
			}
			identity, ok := lateEntries[0].Event.(GitHubObservationEvent)
			if !ok || identity.Sequence != entries[0].Event.(GitHubObservationEvent).Sequence {
				t.Errorf("late observation identity = %#v, want current snapshot sequence", lateEntries[0].Event)
			}
			if count := g.count(t, `SELECT count(*) FROM subscription_match m JOIN entry e
				ON e.inbox_id = $1 AND e.cursor = m.cursor WHERE m.subscription_id = $2
				AND e.event->>'provider' = 'github' AND e.event->>'event' IN ('check_run', 'status')`, g.inbox, first.ID); count != 0 {
				t.Errorf("normalized raw webhook matches = %d, want none for PR", count)
			}
		})
	}
}

func TestGitHubCISnapshotKeepsLatestAttemptPerCheckAppAndCurrentHeadOnly(t *testing.T) {
	g := newGitHubFixture(t)
	source := g.source(PullRequestSubject{Number: 7})
	seed := g.follow(t, source, "seed")
	g.observePullRequest(t, seed, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})
	g.validate(t, g.access())
	for _, test := range []struct {
		sha        string
		checkApp   int64
		runID      int64
		conclusion string
	}{
		{commitA, 202, 301, "success"}, // older attempt for the same App and name
		{commitA, 203, 302, "failure"}, // same name from a different App remains current
		{commitA, 202, 303, "success"}, // newer attempt replaces only App 202's older attempt
		{commitB, 202, 404, "success"}, // historical head state is not a current snapshot
	} {
		observation := checkRunObservation(g, test.sha, "completed", test.conclusion)
		observation.CheckAppID = test.checkApp
		observation.ID = test.runID
		g.ingest(t, normalizedCheckRunDelivery(g, test.sha, observation))
	}
	lateDestination := g.destination
	lateDestination.Name = "late-latest-attempt"
	late := g.followIn(t, lateDestination, source, "late")
	g.reconcileSubscription(t, late)
	entries := observationEntries(t, g.read(t, late.InboxID, 0).Entries)
	if len(entries) != 2 {
		t.Fatalf("late CI snapshot entries = %d, want one latest run per check App on current head", len(entries))
	}
	ids := map[int64]int64{}
	for _, entry := range entries {
		var observation GitHubCIObservation
		if err := json.Unmarshal(entry.Payload, &observation); err != nil {
			t.Fatal(err)
		}
		ids[observation.CheckAppID] = observation.ID
	}
	if ids[202] != 303 || ids[203] != 302 {
		t.Errorf("latest check runs by App = %v, want 202:303 and 203:302", ids)
	}
}

func sameUUIDs(got, want []uuid.UUID) bool {
	if len(got) != len(want) {
		return false
	}
	set := make(map[uuid.UUID]int, len(got))
	for _, id := range got {
		set[id]++
	}
	for _, id := range want {
		set[id]--
	}
	for _, count := range set {
		if count != 0 {
			return false
		}
	}
	return true
}

func TestGitHubCIHonorsActionFiltersAndStatusIDsDoNotRegress(t *testing.T) {
	g := newGitHubFixture(t)
	created := "created"
	source := g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventCheckRun, Actions: []string{created}})
	sub := g.follow(t, source, "created-only")
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})
	queued := checkRunObservation(g, commitA, "queued", "")
	g.pollCI(t, sub.Subject, queued)
	g.reconcileSubscription(t, sub)
	if got := len(observationEntries(t, g.read(t, g.inbox, 0).Entries)); got != 1 {
		t.Fatalf("created-only CI entries = %d, want current queued snapshot", got)
	}

	status := GitHubCIObservation{RepositoryID: g.repository, HeadSHA: commitA, Event: EventStatus, ID: 50, Name: "ci/test", Status: "success"}
	statusSource := g.source(PullRequestSubject{Number: 8}, EventFilter{Event: EventStatus})
	statusSub := g.follow(t, statusSource, "status")
	g.observePullRequest(t, statusSub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})
	g.pollCI(t, statusSub.Subject, status)
	newer := status
	newer.ID++
	newer.Status = "failure"
	g.exec(t, `UPDATE github_ci_refresh SET last_success_at = now() - interval '11 minutes'
		WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3 AND subject_key = $4`,
		g.app, g.installation, g.repository, statusSub.Subject.Key)
	g.pollCI(t, statusSub.Subject, newer)
	older := status
	older.Status = "pending"
	// A newer status row owns this context; delayed older IDs cannot replace or fan out after it.
	lease, err := g.state.Acquire(t.Context(), GitHubCIRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: statusSub.Subject})
	if err != nil || lease == nil {
		// Successful state is fresh. Force the independent CI repair due to model the delayed poll/webhook result.
		g.exec(t, `UPDATE github_ci_refresh SET last_success_at = now() - interval '11 minutes'
			WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3 AND subject_key = $4`,
			g.app, g.installation, g.repository, statusSub.Subject.Key)
		lease, err = g.state.Acquire(t.Context(), GitHubCIRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: statusSub.Subject})
	}
	if err != nil || lease == nil {
		t.Fatalf("status repair lease = %v, %v", lease, err)
	}
	if err := g.state.Succeed(t.Context(), *lease, GitHubSuccess{Fences: []GitHubAccessFence{g.validate(t, g.access())}, CI: []GitHubCIObservation{older}}); err != nil {
		t.Fatal(err)
	}
	var currentID int64
	var currentStatus string
	if err := g.db.QueryRowContext(t.Context(), `SELECT (payload->>'id')::bigint, payload->>'status'
		FROM github_ci_current WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3
		AND head_sha = $4 AND event = 'status' AND entity_key = 'ci/test'`,
		g.app, g.installation, g.repository, commitA).Scan(&currentID, &currentStatus); err != nil {
		t.Fatal(err)
	}
	if currentID != newer.ID || currentStatus != newer.Status {
		t.Errorf("current status = %d/%s, want newer %d/%s", currentID, currentStatus, newer.ID, newer.Status)
	}
}

func TestGitHubCISuccessIsFencedByHeadAndAccessGeneration(t *testing.T) {
	for _, test := range []struct {
		name       string
		invalidate func(*testing.T, githubFixture, githubSubscription)
	}{
		{
			name: "PR head changed",
			invalidate: func(t *testing.T, g githubFixture, sub githubSubscription) {
				g.observePullRequest(t, sub, GitHubPullRequestObservation{
					BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB,
					State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
				})
			},
		},
		{
			name: "installation generation revoked and restored",
			invalidate: func(t *testing.T, g githubFixture, _ githubSubscription) {
				g.deliver(t, "installation", action("removed"))
			},
		},
		{
			name: "newer webhook observation",
			invalidate: func(t *testing.T, g githubFixture, _ githubSubscription) {
				webhookObservation := checkRunObservation(g, commitA, "in_progress", "")
				g.ingest(t, normalizedCheckRunDelivery(g, commitA, webhookObservation))
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "fenced-ci")
			g.observePullRequest(t, sub, GitHubPullRequestObservation{
				BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
				State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
			})
			key := GitHubCIRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: sub.Subject}
			oldLease, err := g.state.Acquire(t.Context(), key)
			if err != nil || oldLease == nil {
				t.Fatalf("initial CI lease = %v, %v", oldLease, err)
			}
			oldFence := g.validate(t, g.access())
			test.invalidate(t, g, sub)

			oldHeadObservation := checkRunObservation(g, commitA, "completed", "success")
			beforeEvents := g.count(t, `SELECT count(*) FROM github_ci_event WHERE app_id = $1`, g.app)
			beforeCurrent := g.count(t, `SELECT count(*) FROM github_ci_current WHERE app_id = $1`, g.app)
			requireDeferred(t, g.state.Succeed(t.Context(), *oldLease, GitHubSuccess{
				Fences: []GitHubAccessFence{oldFence}, CI: []GitHubCIObservation{oldHeadObservation},
			}))
			if got := g.count(t, `SELECT count(*) FROM github_ci_event WHERE app_id = $1`, g.app); got != beforeEvents {
				t.Fatalf("stale CI lease changed event count from %d to %d", beforeEvents, got)
			}
			if got := g.count(t, `SELECT count(*) FROM github_ci_current WHERE app_id = $1`, g.app); got != beforeCurrent {
				t.Fatalf("stale CI lease changed current row count from %d to %d", beforeCurrent, got)
			}

			if test.name == "installation generation revoked and restored" {
				currentFence := g.validate(t, g.access())
				restoredLease, err := g.state.Acquire(t.Context(), key)
				if err != nil || restoredLease == nil {
					t.Fatalf("restored CI lease = %v, %v", restoredLease, err)
				}
				// Restoring access gives a new generation. An older result fence still cannot commit under it.
				requireDeferred(t, g.state.Succeed(t.Context(), *restoredLease, GitHubSuccess{
					Fences: []GitHubAccessFence{oldFence}, CI: []GitHubCIObservation{oldHeadObservation},
				}))
				if got := g.count(t, `SELECT count(*) FROM github_ci_event WHERE app_id = $1`, g.app); got != 0 {
					t.Fatalf("stale access fence appended %d events, want none", got)
				}
				if err := g.state.Succeed(t.Context(), *restoredLease, GitHubSuccess{
					Fences: []GitHubAccessFence{currentFence}, CI: []GitHubCIObservation{oldHeadObservation},
				}); err != nil {
					t.Fatalf("restored access fence did not recover CI refresh: %v", err)
				}
				if got := g.count(t, `SELECT count(*) FROM github_ci_event WHERE app_id = $1`, g.app); got != 1 {
					t.Errorf("recovered CI transitions = %d, want one", got)
				}
			}
		})
	}
}

func TestGitHubCheckRunRerequestCanMoveCompletedBackToInProgress(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}, EventFilter{
		Event: EventCheckRun, Actions: []string{"completed"},
	}), "rerequested-run")
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})
	for _, state := range []struct{ status, conclusion string }{
		{status: "completed", conclusion: "success"},
		{status: "in_progress"},
		{status: "completed", conclusion: "failure"},
	} {
		g.ingest(t, normalizedCheckRunDelivery(g, commitA,
			checkRunObservation(g, commitA, state.status, state.conclusion)))
	}
	if got := g.count(t, `SELECT count(*) FROM github_ci_event WHERE app_id = $1`, g.app); got != 3 {
		t.Fatalf("same-run transition history = %d, want all three A-B-A states", got)
	}
	if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
		t.Fatal(err)
	}
	entries := observationEntries(t, g.read(t, g.inbox, 0).Entries)
	if len(entries) != 2 {
		t.Fatalf("completed-filter inbox transitions = %d, want both completed states", len(entries))
	}
	for _, entry := range entries {
		var observation GitHubCIObservation
		if err := json.Unmarshal(entry.Payload, &observation); err != nil {
			t.Fatal(err)
		}
		if observation.ID != 303 || observation.Status != "completed" {
			t.Errorf("completed filter emitted %+v, want run 303 completed", observation)
		}
	}
}

func TestGitHubCIRetentionKeepsCurrentStateAndInboxCopies(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "retained-ci")
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})
	g.pollCI(t, sub.Subject, checkRunObservation(g, commitA, "completed", "success"))
	if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
		t.Fatal(err)
	}
	if got := len(observationEntries(t, g.read(t, g.inbox, 0).Entries)); got != 1 {
		t.Fatalf("inbox CI observation copies = %d, want one before cleanup", got)
	}

	cutoff := time.Now().Add(-24 * time.Hour)
	g.ingest(t, normalizedCheckRunDelivery(g, commitB,
		checkRunObservation(g, commitB, "completed", "success")))
	g.exec(t, `UPDATE github_ci_event SET created_at = $2 WHERE app_id = $1`, g.app, cutoff.Add(-time.Hour))
	g.exec(t, `UPDATE github_ci_current SET payload = jsonb_set(payload, '{observedAt}', to_jsonb($2::timestamptz))
		WHERE app_id = $1`, g.app, cutoff.Add(-time.Hour))
	if err := g.CleanupGitHubDeliveries(t.Context(), cutoff); err != nil {
		t.Fatal(err)
	}
	if got := g.count(t, `SELECT count(*) FROM github_ci_event WHERE app_id = $1`, g.app); got != 0 {
		t.Errorf("expired CI transitions after cleanup = %d, want none", got)
	}
	if got := g.count(t, `SELECT count(*) FROM github_ci_current WHERE app_id = $1 AND head_sha = $2`, g.app, commitA); got != 1 {
		t.Errorf("current CI state after cleanup = %d rows, want one", got)
	}
	if got := g.count(t, `SELECT count(*) FROM github_ci_current WHERE app_id = $1 AND head_sha = $2`, g.app, commitB); got != 0 {
		t.Errorf("obsolete CI head state after cleanup = %d rows, want none", got)
	}
	if got := len(observationEntries(t, g.read(t, g.inbox, 0).Entries)); got != 1 {
		t.Errorf("inbox CI observation copies after cleanup = %d, want one", got)
	}
}

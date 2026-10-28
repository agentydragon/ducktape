package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"reflect"
	"testing"
	"time"

	"github.com/google/uuid"
)

func checkRunObservation(g githubFixture, sha string, status GitHubCheckRunStatus, conclusion string) GitHubCheckRun {
	run := GitHubCheckRun{RepositoryID: g.repository, HeadSHA: sha, AppID: 202, ID: 303, Name: "unit", Status: status, DetailsURL: "https://checks.example/unit"}
	if conclusion != "" {
		run.Conclusion = &conclusion
	}
	return run
}

func commitStatusObservation(g githubFixture, sha string, id int64, context string, state GitHubCommitStatusState) GitHubCommitStatus {
	return GitHubCommitStatus{RepositoryID: g.repository, SHA: sha, ID: id, Context: context, State: state, TargetURL: "https://statuses.example/" + context}
}

func (g githubFixture) pollCheckRuns(t *testing.T, subject GitHubSubjectKey, runs []GitHubCheckRun) {
	t.Helper()
	g.pollCommitObservations(t, subject, EventCheckRun, runs, nil)
}

func (g githubFixture) pollCommitStatuses(t *testing.T, subject GitHubSubjectKey, statuses []GitHubCommitStatus) {
	t.Helper()
	g.pollCommitObservations(t, subject, EventStatus, nil, statuses)
}

func (g githubFixture) pollCommitObservations(t *testing.T, subject GitHubSubjectKey, event EventName, runs []GitHubCheckRun, statuses []GitHubCommitStatus) {
	t.Helper()
	key := GitHubCommitRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: subject, Event: event}
	lease, err := g.state.Acquire(t.Context(), key)
	if err != nil {
		t.Fatal(err)
	}
	if lease == nil {
		g.exec(t, `UPDATE github_commit_refresh SET last_success_at = now() - interval '11 minutes', next_attempt = NULL, claim = NULL, claim_until = NULL
			WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3 AND subject_key = $4 AND event = $5`,
			g.app, g.installation, subject.RepositoryID, subject.Key, string(event))
		lease, err = g.state.Acquire(t.Context(), key)
	}
	if err != nil || lease == nil {
		t.Fatalf("%s refresh lease = %v, %v", event, lease, err)
	}
	success := GitHubSuccess{Fences: []GitHubAccessFence{g.validate(t, g.access())}}
	if event == EventCheckRun {
		success.CheckRuns = runs
	} else {
		success.CommitStatuses = statuses
	}
	if err := g.state.Succeed(t.Context(), *lease, success); err != nil {
		t.Fatal(err)
	}
}

func normalizedCheckRunDelivery(g githubFixture, run GitHubCheckRun) GitHubIngest {
	action := "created"
	if run.Status == CheckRunInProgress {
		action = "in_progress"
	} else if run.Status == CheckRunCompleted {
		action = "completed"
	}
	ingest := g.delivery(string(EventCheckRun), withAction(action), onPullRequest(7), atCommit(run.HeadSHA))
	ingest.CheckRun = &run
	return ingest
}

func normalizedCommitStatusDelivery(g githubFixture, status GitHubCommitStatus) GitHubIngest {
	ingest := g.delivery(string(EventStatus), onPullRequest(7), atCommit(status.SHA))
	ingest.CommitStatus = &status
	return ingest
}

func commitObservationEntries(t *testing.T, entries []Entry) []Entry {
	t.Helper()
	var observations []Entry
	for _, entry := range entries {
		if identity, ok := entry.Event.(GitHubObservationEvent); ok && (identity.Event == EventCheckRun || identity.Event == EventStatus) {
			observations = append(observations, entry)
		}
	}
	return observations
}

func sameUUIDs(got, want []uuid.UUID) bool {
	if len(got) != len(want) {
		return false
	}
	counts := make(map[uuid.UUID]int, len(got))
	for _, id := range got {
		counts[id]++
	}
	for _, id := range want {
		counts[id]--
	}
	for _, count := range counts {
		if count != 0 {
			return false
		}
	}
	return true
}

func TestGitHubCheckRunPollWebhookDedupAndLateSnapshot(t *testing.T) {
	for _, webhookFirst := range []bool{true, false} {
		t.Run(map[bool]string{true: "webhook first", false: "poll first"}[webhookFirst], func(t *testing.T) {
			g := newGitHubFixture(t)
			source := g.source(PullRequestSubject{Number: 7})
			first := g.follow(t, source, "first")
			second := g.follow(t, source, "second")
			g.observePullRequest(t, first, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
			run := checkRunObservation(g, commitA, CheckRunCompleted, "success")
			pollRun := run
			pollRun.DetailsURL = "https://checks.example/polled-details"
			completedAt := time.Date(2026, 10, 27, 10, 0, 0, 0, time.UTC)
			pollRun.CompletedAt = &completedAt
			if webhookFirst {
				g.ingest(t, normalizedCheckRunDelivery(g, run))
				g.pollCheckRuns(t, first.Subject, []GitHubCheckRun{pollRun})
			} else {
				g.pollCheckRuns(t, first.Subject, []GitHubCheckRun{pollRun})
				g.ingest(t, normalizedCheckRunDelivery(g, run))
			}
			if got := g.count(t, `SELECT count(*) FROM github_commit_event WHERE app_id = $1 AND event = 'check_run'`, g.app); got != 1 {
				t.Fatalf("deduplicated transitions = %d, want one", got)
			}
			for _, sub := range []githubSubscription{first, second} {
				if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
					t.Fatal(err)
				}
			}
			entries := commitObservationEntries(t, g.read(t, g.inbox, 0).Entries)
			if len(entries) != 1 || !sameUUIDs(entries[0].SubscriptionIDs, []uuid.UUID{first.ID, second.ID}) {
				t.Fatalf("shared fact fanout = %+v, want one fact for both subscribers", entries)
			}
			var got GitHubCheckRun
			if err := json.Unmarshal(entries[0].Payload, &got); err != nil {
				t.Fatal(err)
			}
			if got.Status != CheckRunCompleted || got.ID != run.ID || got.HeadSHA != commitA {
				t.Errorf("fact payload = %+v", got)
			}
			var eventPayload, currentPayload []byte
			if err := g.db.QueryRowContext(t.Context(), `SELECT payload FROM github_commit_event WHERE app_id = $1 ORDER BY sequence LIMIT 1`, g.app).Scan(&eventPayload); err != nil {
				t.Fatal(err)
			}
			if err := g.db.QueryRowContext(t.Context(), `SELECT payload FROM github_commit_current WHERE app_id = $1 AND head_sha = $2 AND event = 'check_run' AND entity_key = '303'`, g.app, commitA).Scan(&currentPayload); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(eventPayload, currentPayload) {
				t.Errorf("immutable event payload %s differs from current payload %s", eventPayload, currentPayload)
			}
			lateDestination := g.destination
			lateDestination.Name = "late"
			late := g.followIn(t, lateDestination, source, "late")
			g.reconcileSubscription(t, late)
			lateEntries := commitObservationEntries(t, g.read(t, late.InboxID, 0).Entries)
			if len(lateEntries) != 1 || lateEntries[0].Event.(GitHubObservationEvent).Sequence != entries[0].Event.(GitHubObservationEvent).Sequence {
				t.Errorf("late fact snapshot = %+v, want same current sequence", lateEntries)
			}
		})
	}
}

func TestGitHubCheckRunSnapshotUsesLatestAttemptPerAppOnCurrentHead(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "seed-check-runs")
	g.observePullRequest(t, sub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})

	for _, run := range []GitHubCheckRun{
		{RepositoryID: g.repository, HeadSHA: commitA, ID: 301, AppID: 202, Name: "unit", Status: CheckRunCompleted, Conclusion: stringPointer("success")},
		{RepositoryID: g.repository, HeadSHA: commitA, ID: 302, AppID: 203, Name: "unit", Status: CheckRunCompleted, Conclusion: stringPointer("success")},
		{RepositoryID: g.repository, HeadSHA: commitA, ID: 303, AppID: 202, Name: "unit", Status: CheckRunCompleted, Conclusion: stringPointer("failure")},
		{RepositoryID: g.repository, HeadSHA: commitB, ID: 404, AppID: 202, Name: "unit", Status: CheckRunCompleted, Conclusion: stringPointer("success")},
	} {
		g.ingest(t, normalizedCheckRunDelivery(g, run))
	}

	lateDestination := g.destination
	lateDestination.Name = "latest-check-runs"
	late := g.followIn(t, lateDestination, sub.Source, "latest-check-runs")
	g.reconcileSubscription(t, late)
	entries := commitObservationEntries(t, g.read(t, late.InboxID, 0).Entries)
	if len(entries) != 2 {
		t.Fatalf("current check-run snapshot entries = %d, want two App-owned current attempts: %+v", len(entries), entries)
	}
	got := map[int64]int64{}
	for _, entry := range entries {
		var run GitHubCheckRun
		if err := json.Unmarshal(entry.Payload, &run); err != nil {
			t.Fatal(err)
		}
		got[run.AppID] = run.ID
	}
	if !reflect.DeepEqual(got, map[int64]int64{202: 303, 203: 302}) {
		t.Errorf("current check-run attempts = %v, want App 202 run 303 and App 203 run 302", got)
	}
}

func stringPointer(value string) *string { return &value }

func TestGitHubCommitObservationSelectionAndActionFilters(t *testing.T) {
	t.Run("default check runs only completed", func(t *testing.T) {
		g := newGitHubFixture(t)
		sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "default")
		g.observePullRequest(t, sub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
		g.pollCheckRuns(t, sub.Subject, []GitHubCheckRun{checkRunObservation(g, commitA, CheckRunQueued, "")})
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		if got := len(commitObservationEntries(t, g.read(t, sub.InboxID, 0).Entries)); got != 0 {
			t.Fatalf("default queued facts = %d", got)
		}
		g.pollCheckRuns(t, sub.Subject, []GitHubCheckRun{checkRunObservation(g, commitA, CheckRunCompleted, "success")})
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		entries := commitObservationEntries(t, g.read(t, sub.InboxID, 0).Entries)
		if len(entries) != 1 {
			t.Fatalf("default completed facts = %d, want one", len(entries))
		}
		var run GitHubCheckRun
		if err := json.Unmarshal(entries[0].Payload, &run); err != nil {
			t.Fatal(err)
		}
		if run.Status != CheckRunCompleted {
			t.Errorf("default emitted %s", run.Status)
		}
	})

	t.Run("unfiltered check runs every native status", func(t *testing.T) {
		g := newGitHubFixture(t)
		source := g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventCheckRun})
		sub := g.follow(t, source, "all-states")
		g.observePullRequest(t, sub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
		want := []GitHubCheckRunStatus{CheckRunQueued, CheckRunWaiting, CheckRunPending, CheckRunRequested, CheckRunInProgress, CheckRunCompleted}
		for _, status := range want {
			conclusion := ""
			if status == CheckRunCompleted {
				conclusion = "success"
			}
			g.pollCheckRuns(t, sub.Subject, []GitHubCheckRun{checkRunObservation(g, commitA, status, conclusion)})
		}
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		entries := commitObservationEntries(t, g.read(t, sub.InboxID, 0).Entries)
		var got []GitHubCheckRunStatus
		for _, entry := range entries {
			var run GitHubCheckRun
			if err := json.Unmarshal(entry.Payload, &run); err != nil {
				t.Fatal(err)
			}
			got = append(got, run.Status)
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("native check-run statuses = %v, want %v", got, want)
		}
	})

	t.Run("explicit action filter keeps raw webhook", func(t *testing.T) {
		g := newGitHubFixture(t)
		source := g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventCheckRun, Actions: []string{"created"}})
		sub := g.follow(t, source, "created-action")
		g.observePullRequest(t, sub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
		g.ingest(t, normalizedCheckRunDelivery(g, checkRunObservation(g, commitA, CheckRunQueued, "")))
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		entries := g.read(t, sub.InboxID, 0).Entries
		var raw, normalized int
		for _, entry := range entries {
			switch event := entry.Event.(type) {
			case GitHubEvent:
				if event.Event == EventCheckRun {
					raw++
				}
			case GitHubObservationEvent:
				if event.Event == EventCheckRun {
					normalized++
				}
			}
		}
		if raw != 1 || normalized != 0 {
			t.Errorf("action filter got raw=%d normalized=%d, want raw=1 normalized=0", raw, normalized)
		}
	})

	t.Run("commit status selects generated native facts", func(t *testing.T) {
		g := newGitHubFixture(t)
		source := g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventStatus})
		sub := g.follow(t, source, "statuses")
		g.observePullRequest(t, sub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
		status := commitStatusObservation(g, commitA, 51, "ci/unit", CommitStatusSuccess)
		g.ingest(t, normalizedCommitStatusDelivery(g, status))
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		entries := g.read(t, sub.InboxID, 0).Entries
		if len(commitObservationEntries(t, entries)) != 1 {
			t.Fatalf("generated status facts = %d, want one", len(commitObservationEntries(t, entries)))
		}
		for _, entry := range entries {
			if event, ok := entry.Event.(GitHubEvent); ok && event.Event == EventStatus {
				t.Errorf("normalized raw status was delivered")
			}
		}
	})
}

func TestGitHubCommitStatusIDsContextsAndCheckRunReruns(t *testing.T) {
	g := newGitHubFixture(t)
	subject := GitHubSubjectKey{RepositoryID: g.repository, Kind: "pull_request", Key: "7"}
	statusSub := g.follow(t, g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventStatus}), "statuses")
	g.observePullRequest(t, statusSub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
	g.pollCommitStatuses(t, subject, []GitHubCommitStatus{commitStatusObservation(g, commitA, 50, "ci/unit", CommitStatusPending), commitStatusObservation(g, commitA, 3, "security", CommitStatusFailure)})
	g.pollCommitStatuses(t, subject, []GitHubCommitStatus{commitStatusObservation(g, commitA, 51, "ci/unit", CommitStatusSuccess)})
	g.pollCommitStatuses(t, subject, []GitHubCommitStatus{commitStatusObservation(g, commitA, 50, "ci/unit", CommitStatusError)})
	var currentID int64
	var currentState string
	if err := g.db.QueryRowContext(t.Context(), `SELECT (payload->>'id')::bigint, payload->>'state' FROM github_commit_current
		WHERE app_id = $1 AND repository_id = $2 AND head_sha = $3 AND event = 'status' AND entity_key = 'ci/unit'`, g.app, g.repository, commitA).Scan(&currentID, &currentState); err != nil {
		t.Fatal(err)
	}
	if currentID != 51 || currentState != string(CommitStatusSuccess) {
		t.Errorf("current status = %d/%s, want 51/success", currentID, currentState)
	}
	if got := g.count(t, `SELECT count(*) FROM github_commit_current WHERE app_id = $1 AND event = 'status'`, g.app); got != 2 {
		t.Errorf("current contexts = %d, want two", got)
	}

	runSub := g.follow(t, g.source(PullRequestSubject{Number: 8}, EventFilter{Event: EventCheckRun}), "rerun")
	g.observePullRequest(t, runSub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
	for _, state := range []struct {
		status     GitHubCheckRunStatus
		conclusion string
	}{{CheckRunCompleted, "success"}, {CheckRunInProgress, ""}, {CheckRunCompleted, "failure"}} {
		g.ingest(t, normalizedCheckRunDelivery(g, checkRunObservation(g, commitC, state.status, state.conclusion)))
	}
	if got := g.count(t, `SELECT count(*) FROM github_commit_event WHERE app_id = $1 AND repository_id = $2 AND event = 'check_run'`, g.app, g.repository); got != 3 {
		t.Errorf("same run A-B-A events = %d, want three", got)
	}
}

func TestGitHubCommitRefreshFencesAndCategoryIndependence(t *testing.T) {
	for _, test := range []struct {
		name          string
		keyEvent      EventName
		observedEvent EventName
		wantDeferred  bool
		revoke        bool
	}{
		{"head change fences both", EventCheckRun, EventCheckRun, true, false},
		{"same-category check-run webhook fences poll", EventCheckRun, EventCheckRun, true, false},
		{"status webhook does not fence check runs", EventCheckRun, EventStatus, false, false},
		{"check-run webhook does not fence statuses", EventStatus, EventCheckRun, false, false},
		{"installation revocation fences poll", EventStatus, "", true, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "fenced")
			g.observePullRequest(t, sub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
			key := GitHubCommitRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: sub.Subject, Event: test.keyEvent}
			lease, err := g.state.Acquire(t.Context(), key)
			if err != nil || lease == nil {
				t.Fatalf("lease = %v, %v", lease, err)
			}
			fence := g.validate(t, g.access())
			if test.name == "head change fences both" {
				g.observePullRequest(t, sub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
			} else if test.revoke {
				g.deliver(t, "installation", withAction("removed"))
			} else if test.observedEvent == EventCheckRun {
				g.ingest(t, normalizedCheckRunDelivery(g, checkRunObservation(g, commitA, CheckRunInProgress, "")))
			} else {
				g.ingest(t, normalizedCommitStatusDelivery(g, commitStatusObservation(g, commitA, 88, "ci/unit", CommitStatusSuccess)))
			}
			success := GitHubSuccess{Fences: []GitHubAccessFence{fence}}
			if test.keyEvent == EventCheckRun {
				success.CheckRuns = []GitHubCheckRun{checkRunObservation(g, commitA, CheckRunCompleted, "success")}
			} else {
				success.CommitStatuses = []GitHubCommitStatus{commitStatusObservation(g, commitA, 89, "ci/unit", CommitStatusSuccess)}
			}
			err = g.state.Succeed(t.Context(), *lease, success)
			var deferred *RefreshDeferredError
			if test.wantDeferred && !errors.As(err, &deferred) {
				t.Fatalf("stale result error = %v, want deferred", err)
			}
			if !test.wantDeferred && err != nil {
				t.Fatalf("other event category fenced the lease: %v", err)
			}
			if test.name == "same-category check-run webhook fences poll" {
				if got := g.count(t, `SELECT count(*) FROM github_commit_event WHERE app_id = $1 AND event = 'check_run'`, g.app); got != 1 {
					t.Errorf("same-category events after stale poll = %d, want only the webhook fact", got)
				}
			}
			if test.revoke {
				if got := g.count(t, `SELECT count(*) FROM github_commit_event WHERE app_id = $1`, g.app); got != 0 {
					t.Errorf("events after access revocation = %d, want none", got)
				}
			}
		})
	}
}

func TestGitHubCommitObservationRetentionKeepsCurrentAndInboxCopies(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(PullRequestSubject{Number: 7}), "retained")
	g.observePullRequest(t, sub, GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown})
	g.pollCheckRuns(t, sub.Subject, []GitHubCheckRun{checkRunObservation(g, commitA, CheckRunCompleted, "success")})
	if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
		t.Fatal(err)
	}
	if got := len(commitObservationEntries(t, g.read(t, sub.InboxID, 0).Entries)); got != 1 {
		t.Fatalf("inbox facts before retention = %d", got)
	}
	g.ingest(t, normalizedCheckRunDelivery(g, checkRunObservation(g, commitB, CheckRunCompleted, "success")))
	cutoff := time.Now().Add(-24 * time.Hour)
	g.exec(t, `UPDATE github_commit_event SET created_at = $2 WHERE app_id = $1`, g.app, cutoff.Add(-time.Hour))
	g.exec(t, `UPDATE github_commit_current SET payload = jsonb_set(payload, '{observedAt}', to_jsonb($2::timestamptz)) WHERE app_id = $1`, g.app, cutoff.Add(-time.Hour))
	if err := g.CleanupGitHubDeliveries(t.Context(), cutoff); err != nil {
		t.Fatal(err)
	}
	if got := g.count(t, `SELECT count(*) FROM github_commit_event WHERE app_id = $1`, g.app); got != 0 {
		t.Errorf("retained event log rows = %d", got)
	}
	if got := g.count(t, `SELECT count(*) FROM github_commit_current WHERE app_id = $1 AND head_sha = $2`, g.app, commitA); got != 1 {
		t.Errorf("current head rows = %d, want one", got)
	}
	if got := g.count(t, `SELECT count(*) FROM github_commit_current WHERE app_id = $1 AND head_sha = $2`, g.app, commitB); got != 0 {
		t.Errorf("obsolete head rows = %d, want none", got)
	}
	if got := len(commitObservationEntries(t, g.read(t, sub.InboxID, 0).Entries)); got != 1 {
		t.Errorf("inbox copies after cleanup = %d, want one", got)
	}
	lateDestination := g.destination
	lateDestination.Name = "late"
	late := g.followIn(t, lateDestination, g.source(PullRequestSubject{Number: 7}), "late")
	g.reconcileSubscription(t, late)
	if got := len(commitObservationEntries(t, g.read(t, late.InboxID, 0).Entries)); got != 1 {
		t.Errorf("late current state = %d, want one", got)
	}
}

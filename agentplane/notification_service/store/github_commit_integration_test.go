package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"reflect"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func checkRunObservation(g githubFixture, sha string, status model.GitHubCheckRunStatus, conclusion string) model.GitHubCheckRun {
	run := model.GitHubCheckRun{RepositoryID: g.repository, HeadSHA: sha, AppID: 202, ID: 303, Name: "unit", Status: status, DetailsURL: "https://checks.example/unit"}
	if conclusion != "" {
		run.Conclusion = &conclusion
	}
	return run
}

func commitStatusObservation(g githubFixture, sha string, id int64, context string, state model.GitHubCommitStatusState) model.GitHubCommitStatus {
	return model.GitHubCommitStatus{RepositoryID: g.repository, SHA: sha, ID: id, Context: context, State: state, TargetURL: "https://statuses.example/" + context}
}

func (g githubFixture) pollCheckRuns(t *testing.T, subject GitHubSubjectKey, runs []model.GitHubCheckRun) {
	t.Helper()
	g.pollCommitObservations(t, subject, model.EventCheckRun, runs, nil)
}

func (g githubFixture) pollCommitStatuses(t *testing.T, subject GitHubSubjectKey, statuses []model.GitHubCommitStatus) {
	t.Helper()
	g.pollCommitObservations(t, subject, model.EventStatus, nil, statuses)
}

func (g githubFixture) pollCommitObservations(t *testing.T, subject GitHubSubjectKey, event model.EventName, runs []model.GitHubCheckRun, statuses []model.GitHubCommitStatus) {
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
	fences := []GitHubAccessFence{g.validate(t, g.access())}
	if event == model.EventCheckRun {
		err = g.state.CompleteCheckRuns(t.Context(), *lease, fences, runs)
	} else {
		err = g.state.CompleteCommitStatuses(t.Context(), *lease, fences, statuses)
	}
	if err != nil {
		t.Fatal(err)
	}
}

func normalizedCheckRunDelivery(g githubFixture, run model.GitHubCheckRun) GitHubIngest {
	action := "created"
	if run.Status == model.CheckRunInProgress {
		action = "in_progress"
	} else if run.Status == model.CheckRunCompleted {
		action = "completed"
	}
	ingest := g.delivery(string(model.EventCheckRun), withAction(action), onPullRequest(7), atCommit(run.HeadSHA))
	ingest.CheckRun = &run
	return ingest
}

func normalizedCommitStatusDelivery(g githubFixture, status model.GitHubCommitStatus) GitHubIngest {
	ingest := g.delivery(string(model.EventStatus), onPullRequest(7), atCommit(status.SHA))
	ingest.CommitStatus = &status
	return ingest
}

func commitObservationEntries(t *testing.T, entries []Entry) []Entry {
	t.Helper()
	var observations []Entry
	for _, entry := range entries {
		if identity, ok := entry.Event.(model.GitHubObservationEvent); ok && (identity.Event == model.EventCheckRun || identity.Event == model.EventStatus) {
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
			source := g.source(model.PullRequestSubject{Number: 7})
			first := g.follow(t, source, "first")
			second := g.follow(t, source, "second")
			g.observePullRequest(t, first, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
			run := checkRunObservation(g, commitA, model.CheckRunCompleted, "success")
			pollRun := run
			pollRun.DetailsURL = "https://checks.example/polled-details"
			completedAt := time.Date(2026, 10, 27, 10, 0, 0, 0, time.UTC)
			pollRun.CompletedAt = &completedAt
			if webhookFirst {
				g.ingest(t, normalizedCheckRunDelivery(g, run))
				g.pollCheckRuns(t, first.Subject, []model.GitHubCheckRun{pollRun})
			} else {
				g.pollCheckRuns(t, first.Subject, []model.GitHubCheckRun{pollRun})
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
			var got model.GitHubCheckRun
			if err := json.Unmarshal(entries[0].Payload, &got); err != nil {
				t.Fatal(err)
			}
			if got.Status != model.CheckRunCompleted || got.ID != run.ID || got.HeadSHA != commitA {
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
			if len(lateEntries) != 1 || lateEntries[0].Event.(model.GitHubObservationEvent).Sequence != entries[0].Event.(model.GitHubObservationEvent).Sequence {
				t.Errorf("late fact snapshot = %+v, want same current sequence", lateEntries)
			}
		})
	}
}

func TestGitHubCheckRunSnapshotUsesLatestAttemptPerAppOnCurrentHead(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "seed-check-runs")
	g.observePullRequest(t, sub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})

	for _, run := range []model.GitHubCheckRun{
		{RepositoryID: g.repository, HeadSHA: commitA, ID: 301, AppID: 202, Name: "unit", Status: model.CheckRunCompleted, Conclusion: stringPointer("success")},
		{RepositoryID: g.repository, HeadSHA: commitA, ID: 302, AppID: 203, Name: "unit", Status: model.CheckRunCompleted, Conclusion: stringPointer("success")},
		{RepositoryID: g.repository, HeadSHA: commitA, ID: 303, AppID: 202, Name: "unit", Status: model.CheckRunCompleted, Conclusion: stringPointer("failure")},
		{RepositoryID: g.repository, HeadSHA: commitB, ID: 404, AppID: 202, Name: "unit", Status: model.CheckRunCompleted, Conclusion: stringPointer("success")},
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
		var run model.GitHubCheckRun
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
		sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "default")
		g.observePullRequest(t, sub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
		g.pollCheckRuns(t, sub.Subject, []model.GitHubCheckRun{checkRunObservation(g, commitA, model.CheckRunQueued, "")})
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		if got := len(commitObservationEntries(t, g.read(t, sub.InboxID, 0).Entries)); got != 0 {
			t.Fatalf("default queued facts = %d", got)
		}
		g.pollCheckRuns(t, sub.Subject, []model.GitHubCheckRun{checkRunObservation(g, commitA, model.CheckRunCompleted, "success")})
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		entries := commitObservationEntries(t, g.read(t, sub.InboxID, 0).Entries)
		if len(entries) != 1 {
			t.Fatalf("default completed facts = %d, want one", len(entries))
		}
		var run model.GitHubCheckRun
		if err := json.Unmarshal(entries[0].Payload, &run); err != nil {
			t.Fatal(err)
		}
		if run.Status != model.CheckRunCompleted {
			t.Errorf("default emitted %s", run.Status)
		}
	})

	t.Run("unfiltered check runs every native status", func(t *testing.T) {
		g := newGitHubFixture(t)
		source := g.source(model.PullRequestSubject{Number: 7}, model.EventFilter{Event: model.EventCheckRun})
		sub := g.follow(t, source, "all-states")
		g.observePullRequest(t, sub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
		want := []model.GitHubCheckRunStatus{model.CheckRunQueued, model.CheckRunWaiting, model.CheckRunPending, model.CheckRunRequested, model.CheckRunInProgress, model.CheckRunCompleted}
		for _, status := range want {
			conclusion := ""
			if status == model.CheckRunCompleted {
				conclusion = "success"
			}
			g.pollCheckRuns(t, sub.Subject, []model.GitHubCheckRun{checkRunObservation(g, commitA, status, conclusion)})
		}
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		entries := commitObservationEntries(t, g.read(t, sub.InboxID, 0).Entries)
		var got []model.GitHubCheckRunStatus
		for _, entry := range entries {
			var run model.GitHubCheckRun
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
		source := g.source(model.PullRequestSubject{Number: 7}, model.EventFilter{Event: model.EventCheckRun, Actions: []string{"created"}})
		sub := g.follow(t, source, "created-action")
		g.observePullRequest(t, sub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
		g.ingest(t, normalizedCheckRunDelivery(g, checkRunObservation(g, commitA, model.CheckRunQueued, "")))
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		entries := g.read(t, sub.InboxID, 0).Entries
		var raw, normalized int
		for _, entry := range entries {
			switch event := entry.Event.(type) {
			case model.GitHubEvent:
				if event.Event == model.EventCheckRun {
					raw++
				}
			case model.GitHubObservationEvent:
				if event.Event == model.EventCheckRun {
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
		source := g.source(model.PullRequestSubject{Number: 7}, model.EventFilter{Event: model.EventStatus})
		sub := g.follow(t, source, "statuses")
		g.observePullRequest(t, sub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
		status := commitStatusObservation(g, commitA, 51, "ci/unit", model.CommitStatusSuccess)
		g.ingest(t, normalizedCommitStatusDelivery(g, status))
		if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
			t.Fatal(err)
		}
		entries := g.read(t, sub.InboxID, 0).Entries
		if len(commitObservationEntries(t, entries)) != 1 {
			t.Fatalf("generated status facts = %d, want one", len(commitObservationEntries(t, entries)))
		}
		for _, entry := range entries {
			if event, ok := entry.Event.(model.GitHubEvent); ok && event.Event == model.EventStatus {
				t.Errorf("normalized raw status was delivered")
			}
		}
	})
}

func TestGitHubCommitStatusIDsContextsAndCheckRunReruns(t *testing.T) {
	g := newGitHubFixture(t)
	subject := GitHubSubjectKey{RepositoryID: g.repository, Kind: "pull_request", Key: "7"}
	statusSub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}, model.EventFilter{Event: model.EventStatus}), "statuses")
	g.observePullRequest(t, statusSub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
	g.pollCommitStatuses(t, subject, []model.GitHubCommitStatus{commitStatusObservation(g, commitA, 50, "ci/unit", model.CommitStatusPending), commitStatusObservation(g, commitA, 3, "security", model.CommitStatusFailure)})
	g.pollCommitStatuses(t, subject, []model.GitHubCommitStatus{commitStatusObservation(g, commitA, 51, "ci/unit", model.CommitStatusSuccess)})
	g.pollCommitStatuses(t, subject, []model.GitHubCommitStatus{commitStatusObservation(g, commitA, 50, "ci/unit", model.CommitStatusError)})
	var currentID int64
	var currentState string
	if err := g.db.QueryRowContext(t.Context(), `SELECT (payload->>'id')::bigint, payload->>'state' FROM github_commit_current
		WHERE app_id = $1 AND repository_id = $2 AND head_sha = $3 AND event = 'status' AND entity_key = 'ci/unit'`, g.app, g.repository, commitA).Scan(&currentID, &currentState); err != nil {
		t.Fatal(err)
	}
	if currentID != 51 || currentState != string(model.CommitStatusSuccess) {
		t.Errorf("current status = %d/%s, want 51/success", currentID, currentState)
	}
	if got := g.count(t, `SELECT count(*) FROM github_commit_current WHERE app_id = $1 AND event = 'status'`, g.app); got != 2 {
		t.Errorf("current contexts = %d, want two", got)
	}

	runSub := g.follow(t, g.source(model.PullRequestSubject{Number: 8}, model.EventFilter{Event: model.EventCheckRun}), "rerun")
	g.observePullRequest(t, runSub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
	for _, state := range []struct {
		status     model.GitHubCheckRunStatus
		conclusion string
	}{{model.CheckRunCompleted, "success"}, {model.CheckRunInProgress, ""}, {model.CheckRunCompleted, "failure"}} {
		g.ingest(t, normalizedCheckRunDelivery(g, checkRunObservation(g, commitC, state.status, state.conclusion)))
	}
	if got := g.count(t, `SELECT count(*) FROM github_commit_event WHERE app_id = $1 AND repository_id = $2 AND event = 'check_run'`, g.app, g.repository); got != 3 {
		t.Errorf("same run A-B-A events = %d, want three", got)
	}
}

func TestGitHubCommitRefreshFencesAndCategoryIndependence(t *testing.T) {
	for _, test := range []struct {
		name          string
		keyEvent      model.EventName
		observedEvent model.EventName
		wantDeferred  bool
		revoke        bool
	}{
		{"head change fences both", model.EventCheckRun, model.EventCheckRun, true, false},
		{"same-category check-run webhook fences poll", model.EventCheckRun, model.EventCheckRun, true, false},
		{"status webhook does not fence check runs", model.EventCheckRun, model.EventStatus, false, false},
		{"check-run webhook does not fence statuses", model.EventStatus, model.EventCheckRun, false, false},
		{"installation revocation fences poll", model.EventStatus, "", true, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "fenced")
			g.observePullRequest(t, sub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
			key := GitHubCommitRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: sub.Subject, Event: test.keyEvent}
			lease, err := g.state.Acquire(t.Context(), key)
			if err != nil || lease == nil {
				t.Fatalf("lease = %v, %v", lease, err)
			}
			fence := g.validate(t, g.access())
			if test.name == "head change fences both" {
				g.observePullRequest(t, sub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
			} else if test.revoke {
				g.deliver(t, "installation", withAction("removed"))
			} else if test.observedEvent == model.EventCheckRun {
				g.ingest(t, normalizedCheckRunDelivery(g, checkRunObservation(g, commitA, model.CheckRunInProgress, "")))
			} else {
				g.ingest(t, normalizedCommitStatusDelivery(g, commitStatusObservation(g, commitA, 88, "ci/unit", model.CommitStatusSuccess)))
			}
			fences := []GitHubAccessFence{fence}
			if test.keyEvent == model.EventCheckRun {
				err = g.state.CompleteCheckRuns(t.Context(), *lease, fences, []model.GitHubCheckRun{checkRunObservation(g, commitA, model.CheckRunCompleted, "success")})
			} else {
				err = g.state.CompleteCommitStatuses(t.Context(), *lease, fences, []model.GitHubCommitStatus{commitStatusObservation(g, commitA, 89, "ci/unit", model.CommitStatusSuccess)})
			}
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
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "retained")
	g.observePullRequest(t, sub, model.GitHubPullRequestObservation{BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB, State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown})
	g.pollCheckRuns(t, sub.Subject, []model.GitHubCheckRun{checkRunObservation(g, commitA, model.CheckRunCompleted, "success")})
	if err := g.reconcile(t, g.claimInbox(t, sub.InboxID), sub); err != nil {
		t.Fatal(err)
	}
	if got := len(commitObservationEntries(t, g.read(t, sub.InboxID, 0).Entries)); got != 1 {
		t.Fatalf("inbox facts before retention = %d", got)
	}
	g.ingest(t, normalizedCheckRunDelivery(g, checkRunObservation(g, commitB, model.CheckRunCompleted, "success")))
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
	late := g.followIn(t, lateDestination, g.source(model.PullRequestSubject{Number: 7}), "late")
	g.reconcileSubscription(t, late)
	if got := len(commitObservationEntries(t, g.read(t, late.InboxID, 0).Entries)); got != 1 {
		t.Errorf("late current state = %d, want one", got)
	}
}

func TestTypedCompletionRejectsAnotherResourceWithoutConsumingTheLease(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "typed-completion")
	g.observePullRequest(t, sub, model.GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeabilityUnknown,
	})
	key := GitHubCommitRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: sub.Subject, Event: model.EventCheckRun}
	lease, err := g.state.Acquire(t.Context(), key)
	if err != nil || lease == nil {
		t.Fatalf("check-run lease = %v, %v", lease, err)
	}
	fences := []GitHubAccessFence{g.validate(t, g.access())}
	for name, complete := range map[string]func() error{
		"commit statuses": func() error { return g.state.CompleteCommitStatuses(t.Context(), *lease, fences, nil) },
		"reviews":         func() error { return g.state.CompleteReviews(t.Context(), *lease, fences, nil) },
		"subject":         func() error { return g.state.CompleteSubject(t.Context(), *lease, fences, GitHubSubjectObservation{}) },
		"access":          func() error { return g.state.CompleteAccess(t.Context(), *lease, nil) },
	} {
		if err := complete(); err == nil {
			t.Errorf("%s completion accepted a check-run lease", name)
		}
	}
	// Each rejected transaction must leave the original lease usable. A typed completion represents a read,
	// so an empty result is valid without a separate nil-versus-empty marker.
	if err := g.state.CompleteCheckRuns(t.Context(), *lease, fences, nil); err != nil {
		t.Fatal(err)
	}
	state, err := g.state.CommitState(t.Context(), key)
	if err != nil {
		t.Fatal(err)
	}
	if state.LastSuccessAt == nil {
		t.Error("empty check-run read did not settle the original lease")
	}
}

package github

import (
	"encoding/json"
	"fmt"
	"net/http"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

const checkRunsPath = "/repos/owner/repo/commits/" + testSHA + "/check-runs"

func completedCheckRun(id int64) string {
	return fmt.Sprintf(`{"total_count":1,"check_runs":[{"id":%d,"name":"build","head_sha":%q,"status":"completed","conclusion":"success","details_url":"https://github.example.test/checks/%d","started_at":"2026-10-27T00:00:00Z","completed_at":"2026-10-27T00:01:00Z"}]}`, id, testSHA, id)
}

func TestCheckRunsRepairAMissedWebhookAfterRestartAndSharesTheRead(t *testing.T) {
	h := newHarness(t)
	first := h.follow(pullRequestSource(7), "first")
	second := h.follow(pullRequestSource(7), "second")
	claim := h.claim()
	h.reconcileAll(claim)
	if entries := h.page(first.InboxID).Entries; len(entries) != 0 {
		t.Fatalf("initial entries = %d, want none", len(entries))
	}

	// GitHub changes; no webhook arrives. The durable check-run deadline survives a new provider.
	h.fake.respond(checkRunsPath, http.StatusOK, nil, completedCheckRun(31))
	h.exec(`UPDATE github_commit_refresh SET last_success_at = now() - interval '20 minutes'`)
	h.exec(`UPDATE subscription SET next_attempt = now()`)
	h.restart()
	h.reconcileAll(claim)
	entries := h.page(first.InboxID).Entries
	if len(entries) != 1 {
		t.Fatalf("entries = %d, want the recovered check", len(entries))
	}
	event, ok := entries[0].Event.(store.GitHubObservationEvent)
	if !ok || event.Event != store.EventCheckRun || event.RepositoryID != baseRepository || event.Sequence <= 0 {
		t.Fatalf("event = %#v, want a generated check observation", entries[0].Event)
	}
	requireEqual(t, entries[0].SubscriptionIDs, sortedIDs(first.ID, second.ID))
	if got := h.fake.count(checkRunsPath); got != 2 {
		t.Fatalf("check reads = %d, want initial + one shared repair", got)
	}

	// An ordinary wakeup neither polls again nor duplicates the recovered result.
	h.ingest("issue_comment", comment(7))
	h.reconcileAll(claim)
	if got := h.fake.count(checkRunsPath); got != 2 {
		t.Fatalf("check reads after wake = %d, want 2", got)
	}
	if got := len(h.page(first.InboxID).Entries); got != 2 {
		t.Fatalf("entries after comment = %d, want check + comment", got)
	}
}

func TestCheckRunPermissionFailureDoesNotBlockOrdinaryWebhookDelivery(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7), "follow")
	h.fake.respond(checkRunsPath, http.StatusForbidden, nil, `{"message":"Resource not accessible by integration"}`)
	h.ingest("issue_comment", comment(7))
	claim := h.claim()
	h.reconcileAll(claim)
	if got := len(h.page(subscription.InboxID).Entries); got != 1 {
		t.Fatalf("entries = %d, want the ordinary comment", got)
	}
	view := h.subscription(subscription.ID)
	if view.GitHub == nil || view.GitHub.CheckRuns == nil || view.GitHub.CheckRuns.Failure == nil || view.GitHub.CheckRuns.Failure.Kind != store.FailureAccessDenied {
		t.Fatalf("GitHub status = %+v, want independent check-run access failure", view.GitHub)
	}
	if view.GitHub.Subject.Failure != nil || view.Failure != nil {
		t.Fatalf("Check-run denial escaped its coverage status: %+v", view)
	}
}

func TestCommentOnlySubscriptionDoesNotPollCommitResources(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7, store.EventFilter{Event: store.EventIssueComment}), "comments")
	h.ingest("issue_comment", comment(7))
	h.reconcileAll(h.claim())
	if got := h.fake.count(checkRunsPath); got != 0 {
		t.Fatalf("Check-run reads = %d for a comment-only subscription", got)
	}
	if got := len(h.page(subscription.InboxID).Entries); got != 1 {
		t.Fatalf("entries = %d, want comment", got)
	}
}

func TestPendingMergeabilityDoesNotAccelerateCommitResourceReads(t *testing.T) {
	h := newHarness(t)
	h.fake.respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
		`{"number":7,"state":"open","merged":false,"base":{"ref":"main","sha":"`+testBaseSHA+`"},"mergeable":null,"head":{"sha":"`+testSHA+`","repo":{"id":100,"full_name":"owner/repo"}}}`)
	h.follow(pullRequestSource(7), "follow")
	claim := h.claim()
	h.reconcileAll(claim)
	h.exec(`UPDATE github_subject SET next_attempt = now()`)
	h.exec(`UPDATE subscription SET next_attempt = now()`)
	h.reconcileAll(claim)
	if got := h.fake.count(checkRunsPath); got != 1 {
		t.Fatalf("Check-run reads = %d, want one across pending mergeability rechecks", got)
	}
}

func TestPollingAndACompleteWebhookProduceOneCheckObservation(t *testing.T) {
	for _, webhookFirst := range []bool{true, false} {
		t.Run(fmt.Sprintf("webhook_first_%t", webhookFirst), func(t *testing.T) {
			h := newHarness(t)
			subscription := h.follow(pullRequestSource(7), "follow")
			claim := h.claim()
			h.reconcileAll(claim)
			payload := check(testSHA)
			payload["check_run"] = map[string]any{
				"id": 31, "name": "build", "head_sha": testSHA,
				"status": "completed", "conclusion": "success", "pull_requests": []any{},
			}
			poll := func() {
				h.fake.respond(checkRunsPath, http.StatusOK, nil, completedCheckRun(31))
				h.exec(`UPDATE github_commit_refresh SET last_success_at = now() - interval '20 minutes'`)
				h.exec(`UPDATE subscription SET next_attempt = now()`)
				h.reconcileAll(claim)
			}
			webhook := func() {
				h.ingest("check_run", payload)
				h.reconcileAll(claim)
			}
			if webhookFirst {
				webhook()
				poll()
			} else {
				poll()
				webhook()
			}
			if got := len(h.page(subscription.InboxID).Entries); got != 1 {
				t.Fatalf("entries = %d, want one check observation across both paths", got)
			}
		})
	}
}

const commitStatusesPath = "/repos/owner/repo/commits/" + testSHA + "/status"

func successfulCommitStatus(id int64) string {
	return fmt.Sprintf(`{"sha":%q,"state":"success","total_count":1,"statuses":[{"id":%d,"context":"build/status","state":"success","target_url":"https://github.example.test/status"}]}`, testSHA, id)
}

func TestCommitResourcePermissionFailuresAreIndependent(t *testing.T) {
	for _, failChecks := range []bool{true, false} {
		t.Run(fmt.Sprintf("fail_checks_%t", failChecks), func(t *testing.T) {
			h := newHarness(t)
			subscription := h.follow(pullRequestSource(7), "follow")
			h.fake.respond(checkRunsPath, http.StatusOK, nil, completedCheckRun(31))
			h.fake.respond(commitStatusesPath, http.StatusOK, nil, successfulCommitStatus(41))
			failedPath := commitStatusesPath
			wantEvent := store.EventCheckRun
			if failChecks {
				failedPath = checkRunsPath
				wantEvent = store.EventStatus
			}
			h.fake.respond(failedPath, http.StatusForbidden, nil, `{"message":"Resource not accessible by integration"}`)
			h.ingest("issue_comment", comment(7))
			h.reconcileAll(h.claim())
			entries := h.page(subscription.InboxID).Entries
			if len(entries) != 2 {
				t.Fatalf("entries = %d, want comment plus independently recovered resource", len(entries))
			}
			found := false
			for _, entry := range entries {
				if event, ok := entry.Event.(store.GitHubObservationEvent); ok && event.Event == wantEvent {
					found = true
				}
			}
			if !found {
				t.Fatalf("no independently recovered %s observation", wantEvent)
			}
			view := h.subscription(subscription.ID)
			failed, succeeded := view.GitHub.CommitStatuses, view.GitHub.CheckRuns
			if failChecks {
				failed, succeeded = succeeded, failed
			}
			if failed == nil || failed.Failure == nil || failed.Failure.Kind != store.FailureAccessDenied {
				t.Fatalf("failed refresh = %+v, want independent access error", failed)
			}
			if succeeded == nil || succeeded.LastSuccessAt == nil || succeeded.Failure != nil {
				t.Fatalf("successful refresh = %+v, want independent success", succeeded)
			}
			if view.Failure != nil || view.GitHub.Subject.Failure != nil {
				t.Fatalf("resource failure escaped into subscription: %+v", view)
			}
		})
	}
}

func TestCommitResourceSelectionOnlyReadsTheSelectedAPI(t *testing.T) {
	for _, selected := range []store.EventName{store.EventCheckRun, store.EventStatus} {
		t.Run(string(selected), func(t *testing.T) {
			h := newHarness(t)
			h.follow(pullRequestSource(7, store.EventFilter{Event: selected}), "follow")
			h.reconcileAll(h.claim())
			wantChecks, wantStatuses := 0, 0
			if selected == store.EventCheckRun {
				wantChecks = 1
			} else {
				wantStatuses = 1
			}
			if h.fake.count(checkRunsPath) != wantChecks || h.fake.count(commitStatusesPath) != wantStatuses {
				t.Fatalf("API reads = checks:%d statuses:%d, want %d/%d", h.fake.count(checkRunsPath), h.fake.count(commitStatusesPath), wantChecks, wantStatuses)
			}
		})
	}
}

func TestCheckRunStatesRemainDistinctAndOnlyDefaultCompletionNotifies(t *testing.T) {
	h := newHarness(t)
	defaults := h.follow(pullRequestSource(7), "defaults")
	allStates := h.follow(pullRequestSource(7, store.EventFilter{Event: store.EventCheckRun}), "all-states")
	claim := h.claim()
	for index, status := range []string{"queued", "waiting", "in_progress", "completed"} {
		conclusion := ""
		if status == "completed" {
			conclusion = `,"conclusion":"success"`
		}
		h.fake.respond(checkRunsPath, http.StatusOK, nil, fmt.Sprintf(`{"total_count":1,"check_runs":[{"id":31,"name":"build","head_sha":%q,"status":%q%s}]}`, testSHA, status, conclusion))
		h.exec(`UPDATE github_commit_refresh SET last_success_at = now() - interval '20 minutes' WHERE event = 'check_run'`)
		h.exec(`UPDATE subscription SET next_attempt = now()`)
		h.reconcileAll(claim)
		entries := h.page(defaults.InboxID).Entries
		if len(entries) != index+1 {
			t.Fatalf("after %s: entries = %d, want %d distinct native states", status, len(entries), index+1)
		}
		var run store.GitHubCheckRun
		if err := json.Unmarshal(entries[index].Payload, &run); err != nil {
			t.Fatal(err)
		}
		if string(run.Status) != status {
			t.Fatalf("observed status = %s, want %s", run.Status, status)
		}
		var payload map[string]json.RawMessage
		if err := json.Unmarshal(entries[index].Payload, &payload); err != nil {
			t.Fatal(err)
		}
		if _, invented := payload["action"]; invented {
			t.Fatalf("native check-run fact invented webhook action: %s", entries[index].Payload)
		}
		wantSubscriptions := sortedIDs(allStates.ID)
		if status == "completed" {
			wantSubscriptions = sortedIDs(defaults.ID, allStates.ID)
		}
		requireEqual(t, entries[index].SubscriptionIDs, wantSubscriptions)
	}
}

func TestExplicitCheckRunActionsSelectActualWebhookActivity(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7, store.EventFilter{Event: store.EventCheckRun, Actions: []string{"created"}}), "created")
	payload := check(testSHA)
	payload["action"] = "created"
	payload["check_run"] = map[string]any{"id": 31, "name": "build", "head_sha": testSHA, "status": "waiting"}
	h.ingest("check_run", payload)
	h.reconcileAll(h.claim())
	entries := h.page(subscription.InboxID).Entries
	if len(entries) != 1 {
		t.Fatalf("entries = %d, want actual created webhook only", len(entries))
	}
	event, ok := entries[0].Event.(store.GitHubEvent)
	if !ok || event.Action == nil || *event.Action != "created" {
		t.Fatalf("event = %#v, want native created activity", entries[0].Event)
	}
	if h.fake.count(checkRunsPath) != 0 {
		t.Fatal("an activity-only action subscription polled check state")
	}
}
